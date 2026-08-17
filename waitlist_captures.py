"""
waitlist_captures.py — take the money a promotion committed to.

A player joins a paid waitlist and their card is AUTHORISED. A place opens,
`promote_waitlist` admits them and records the hold as an ATTEMPT. Nothing in
Postgres can reach Stripe, so this captures it.

    cd /app && /app/venv/bin/python3 waitlist_captures.py --dry-run
    cd /app && /app/venv/bin/python3 waitlist_captures.py

Runs every minute. A promoted player is `approved` before the money moves, so
the gap between promotion and capture is what the roster shows as "joining" —
the shorter it is, the less that state matters.

══ THE PAYMENTINTENT IS THE FINANCIAL IDENTITY ════════════════════════════

    capture by intent · interpret by intent · settle by intent

`session_participants.payment_intent_id` is only a POINTER to the agreement a
player currently holds, and a rejoin moves it. An earlier design settled by
participant, so a capture that completed while the player rejoined found the
row pointing at a different hold and recorded the money nowhere.

`waitlist_payment_attempts` gives each intent a durable row keyed on the
intent itself. This worker never passes a participant id to a settlement
function; it passes an intent. The participant id in the queue is for logging
and nothing else.

══ IT CAPTURES; IT NEVER CHARGES ══════════════════════════════════════════
`PaymentIntent.capture` with NO amount, which takes the full authorised sum —
whatever the player agreed to when they joined the queue. Deriving an amount
from the session's price would take whatever it costs NOW, so a $10 hold on a
session since repriced to $15 would attempt $15 nobody authorised.
`waitlist_captures_due()` deliberately returns no price.

══ FOUR OUTCOMES, NOT TWO ═════════════════════════════════════════════════
    Stripe captured           settle success with `amount_received`
    already captured          settle success — almost always this worker, cut
                              off before it could record the result
    card declined, expired,   settle failure. The FIRST costs the player their
    cancelled                 place but keeps their position; the second
                              rejects them. The database decides which.
    anything ambiguous        NEITHER. Left for the next run.

That last class is the one worth being careful about. A dropped connection can
mean the request never arrived, or that it arrived and the reply was lost —
indistinguishable from here. Since the second failure REJECTS somebody,
spending one on an outage would eject a player whose card was fine. So the
invariant is not "no capture happened"; it is "we do not know, so we do not
penalise, and the next run reconciles against the intent's own state."

══ WHAT THIS DOES NOT DO ══════════════════════════════════════════════════
Releasing abandoned holds. `expire_holds.py` owns every uncaptured
authorisation, including the ones `holds_to_release()` finds. Two hold-release
implementations is one for somebody to miss.
"""
import os
import sys
from datetime import datetime, timezone

import httpx
import stripe

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

DRY = "--dry-run" in sys.argv

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# A STATE CONFLICT IS NOT A CONCLUSION. Stripe refused the operation given the
# intent's current state — captured, cancelled, or never confirmed. Which of
# those decides whether money moved, so the intent is RETRIEVED and read. The
# message only says to go and look.
CONFLICT = ("already been captured", "already captured", "unexpected_state")

# States that mean the hold is gone or was never usable: a real payment
# failure, and the player's strike.
DEAD = ("canceled", "cancelled", "requires_payment_method")


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def rpc(client, name, payload=None):
    r = client.post(f"{SUPABASE_URL}/rest/v1/rpc/{name}",
                    headers=HEADERS, json=payload or {})
    r.raise_for_status()
    return r.json() if r.text else None


def settle(client, intent, ok, amount=None, failure=None):
    """The ONLY way this worker records anything. Keyed on the intent, never
    on the participant — see the note at the top."""
    rpc(client, "settle_waitlist_capture", {
        "p_intent": intent, "p_ok": ok,
        "p_amount": amount, "p_failure": failure,
    })


def connected_account(client, host_id, cache):
    """The host's Stripe account. A direct charge lives there, so the capture
    must name it."""
    if host_id in cache:
        return cache[host_id]
    r = client.get(
        f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{host_id}&select=stripe_account_id",
        headers=HEADERS,
    )
    rows = r.json() if r.status_code == 200 else []
    cache[host_id] = rows[0].get("stripe_account_id") if rows else None
    return cache[host_id]


def main():
    if not (SUPABASE_URL and SUPABASE_KEY and STRIPE_SECRET_KEY):
        log("ERROR missing SUPABASE_URL, SUPABASE_SERVICE_KEY or STRIPE_SECRET_KEY")
        return 1
    stripe.api_key = STRIPE_SECRET_KEY

    with httpx.Client(timeout=30.0) as client:
        due = rpc(client, "waitlist_captures_due") or []
        if not due:
            log("nothing to capture")
            return 0
        log(f"{len(due)} promotion(s) awaiting capture")

        accounts = {}
        took = failed = deferred = 0

        for d in due:
            intent = d["payment_intent_id"]
            # Logging only. Nothing financial is keyed on this.
            who = d.get("participant_id")
            label = f"{intent} (attempt {d['attempts'] + 1}, participant {who})"

            acct = connected_account(client, d.get("host_id"), accounts)
            if not acct:
                log(f"DEFERRED {label} — host has no Stripe account")
                deferred += 1
                continue

            if DRY:
                log(f"WOULD CAPTURE {intent} on {acct}")
                took += 1
                continue

            try:
                pi = stripe.PaymentIntent.capture(intent, stripe_account=acct)
                settle(client, intent, True, (pi.amount_received or 0) / 100)
                log(f"CAPTURED {label} ${(pi.amount_received or 0) / 100}")
                took += 1
                continue

            # ══ ORDER MATTERS, AND IT IS THE WHOLE PROTECTION ══════════════
            # `APIConnectionError` and `RateLimitError` are SUBCLASSES of
            # `StripeError`. Below the general handler they are unreachable,
            # and an outage becomes `settle(..., False)` — a strike against a
            # player whose card was fine, and the second one rejects them.
            #
            # An earlier revision of this file had them in that order, under a
            # comment claiming the opposite.
            except (stripe.error.APIConnectionError, stripe.error.RateLimitError) as e:
                # We do not know whether the capture happened. No strike.
                log(f"DEFERRED {label} — {type(e).__name__}")
                deferred += 1
                continue

            except stripe.error.StripeError as e:
                detail = str(e)

                if not any(x in detail.lower() for x in CONFLICT):
                    settle(client, intent, False, failure=detail[:400])
                    log(f"FAILED {label} — {detail[:90]}")
                    failed += 1
                    continue

                # ── the intent is the evidence ────────────────────────────
                try:
                    pi = stripe.PaymentIntent.retrieve(intent, stripe_account=acct)
                except stripe.error.StripeError:
                    log(f"DEFERRED {label} — conflict, intent unreadable")
                    deferred += 1
                    continue

                if pi.status == "succeeded" and (pi.amount_received or 0) > 0:
                    settle(client, intent, True, pi.amount_received / 100)
                    log(f"ALREADY CAPTURED {label} — recorded")
                    took += 1
                elif pi.status in DEAD:
                    settle(client, intent, False, failure=f"intent is {pi.status}")
                    log(f"FAILED {label} — intent is {pi.status}")
                    failed += 1
                else:
                    # `processing`, `requires_confirmation`, anything else:
                    # not a card failure and not a capture.
                    log(f"DEFERRED {label} — intent is {pi.status}")
                    deferred += 1

        log(f"done — {took} captured, {failed} failed, {deferred} deferred"
            + (" (dry run)" if DRY else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
