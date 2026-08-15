# ── paste this over the existing stripe_webhook function ──────────────────
@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """What Stripe tells us that we did not ask about.

    Everything else in this file is a question we put to Stripe. This is the
    reverse, and it is the only way to learn things nobody can poll for: a
    player disputing a charge, a refund failing at the bank, a payment coming
    apart after the fact.

    IT NO LONGER WRITES `paid`. `/api/stripe/capture` does that synchronously,
    at the moment the money actually moves. This used to write the same three
    fields on `payment_intent.succeeded` — which under authorise-then-capture
    fires at capture, so the same fact had two authors and one of them arrived
    seconds or minutes late. A late write lands on a row whose situation may
    have changed since, and `refund_due_on_lost_place` fires on `paid IS TRUE`.

    So the branch became a RECONCILIATION: Stripe says the money moved, and if
    the row disagrees that is worth knowing rather than worth overwriting.

    CONNECTED ACCOUNTS. Payments are direct charges on the host's account, so
    these events arrive with `event.account` set. The signature is verified the
    same way; the account id is carried through for logging because a payment
    problem belongs to a particular host.
    """
    from fastapi import Request
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    svc_headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    kind = event["type"]
    acct = event.get("account")          # set for connected-account events
    obj = event["data"]["object"]

    async def notify(user_id: str, ntype: str, message: str, link=None):
        """The same notifications table Host OS reads."""
        if not user_id:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/notifications",
                headers=svc_headers,
                json={"user_id": user_id, "type": ntype,
                      "message": message, "link": link},
            )

    async def participant_by_intent(intent_id: str):
        if not intent_id:
            return None
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/session_participants"
                f"?payment_intent_id=eq.{intent_id}"
                "&select=id,session_id,user_id,full_name,paid,status",
                headers=svc_headers,
            )
            rows = r.json() if r.status_code == 200 else []
            return rows[0] if rows else None

    async def host_of(session_id: str):
        if not session_id:
            return None, None
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/created_sessions?id=eq.{session_id}"
                "&select=user_id,title",
                headers=svc_headers,
            )
            rows = r.json() if r.status_code == 200 else []
            if not rows:
                return None, None
            return rows[0].get("user_id"), rows[0].get("title")

    # ── the money moved, and the row should already say so ────────────────
    if kind == "payment_intent.succeeded":
        p = await participant_by_intent(obj["id"])
        if p and not p.get("paid"):
            # Not corrected here. Capture writes `paid`, and a row that
            # disagrees means capture succeeded at Stripe and failed to record
            # it — a real inconsistency that wants a person, not a silent
            # patch that would hide how often it happens.
            print(f"[webhook] RECONCILE {obj['id']} acct={acct} — Stripe says "
                  f"succeeded, participant {p['id']} says paid=false", flush=True)

    # ── a player has taken their money back ───────────────────────────────
    elif kind == "charge.dispute.created":
        intent_id = obj.get("payment_intent")
        p = await participant_by_intent(intent_id)
        if p:
            host_id, title = await host_of(p["session_id"])
            who = p.get("full_name") or "A player"
            # THE HOST DECIDES. A dispute is a payment event, not a ruling on
            # whether someone plays — they may be someone the host knows, and
            # a dispute can be a mistake. So the place is left alone and the
            # host is told.
            await notify(
                host_id, "payment_disputed",
                f"{who} has disputed their payment for "
                f"{title or 'a session'}. Stripe has taken the money back from "
                f"your account. They still hold their place — remove them if "
                f"you want to.",
            )
            print(f"[webhook] DISPUTE {intent_id} acct={acct} "
                  f"participant={p['id']} session={p['session_id']}", flush=True)
        else:
            print(f"[webhook] DISPUTE {intent_id} acct={acct} — no participant "
                  f"row matches this charge", flush=True)

    # ── a refund landed; the ledger should already know ───────────────────
    elif kind == "charge.refunded":
        intent_id = obj.get("payment_intent")
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/refunds"
                f"?payment_intent_id=eq.{intent_id}&select=id,status",
                headers=svc_headers,
            )
            rows = r.json() if r.status_code == 200 else []
        if not rows:
            # A refund issued outside the ledger — from the Stripe dashboard,
            # most likely. Worth seeing: the ledger is meant to be the only
            # path, and this is how you learn it is not.
            print(f"[webhook] REFUND OUTSIDE LEDGER {intent_id} acct={acct}",
                  flush=True)
        elif rows[0]["status"] != "refunded":
            print(f"[webhook] RECONCILE refund {intent_id} acct={acct} — Stripe "
                  f"refunded, ledger says {rows[0]['status']}", flush=True)

    # ── a payment came apart ──────────────────────────────────────────────
    elif kind == "payment_intent.payment_failed":
        p = await participant_by_intent(obj["id"])
        err = (obj.get("last_payment_error") or {}).get("message", "")
        print(f"[webhook] PAYMENT FAILED {obj['id']} acct={acct} "
              f"participant={p['id'] if p else 'none'} — {err}", flush=True)

    # ── onboarding finished, without anyone asking ────────────────────────
    elif kind == "account.updated":
        account = obj
        if account.get("charges_enabled") and account.get("payouts_enabled"):
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/profiles?stripe_account_id=eq.{account['id']}",
                    headers=svc_headers,
                    json={"stripe_onboarded": True},
                )

    return {"received": True}
