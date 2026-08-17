"""
expire_holds.py — release authorisations nobody is going to act on.

A session requiring approval AUTHORISES at request: Stripe reserves the funds
and takes nothing until the host approves. That is the right shape, and it has
one failure mode — a host who never answers.

    the session starts     the place is gone and the money is still held
    six days pass          Stripe releases the hold on its own at about seven,
                           silently, leaving the request `pending` against a
                           dead intent that a later approve would fail to
                           capture

Both mean the same thing: the request will not be answered usefully, so the
player's money should go back to them now rather than sit reserved.

NO HOST IS PRESENT for either, which is why this cannot live in Host OS. A
refund the host must press is not a policy, and neither is a release.

CANCEL FIRST, THEN THE STATUS — the reverse of an interactive decline. There
is no decision to preserve: if the cancel fails, leaving the row `pending`
means the next run tries again, whereas marking it rejected would strand the
hold with nothing left to notice it.

Also REPORTS, without acting: approved participants whose payment was never
captured. That is money a host is owed for a place already given, and
capturing on their behalf without them deciding is a larger step than
releasing a hold nobody claimed. It is printed so a person can look.

    cd /app && /app/venv/bin/python3 expire_holds.py --dry-run
    cd /app && /app/venv/bin/python3 expire_holds.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
import stripe

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
# NO HARDCODED FALLBACK. The service key bypasses RLS entirely, and a default
# baked into a file that is committed to a repo is that key published.
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

# Stripe releases an uncaptured authorisation at around seven days, varying by
# card and configuration. Six leaves a day of margin: sweeping early costs the
# player nothing — their money returns either way — while sweeping late means
# the hold lapsed on its own and the row still claims to be live.
HOLD_DAYS = 6

DRY = "--dry-run" in sys.argv

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def session_started(sess) -> bool:
    """Melbourne local time, matching every other date rule in this schema."""
    date_s, time_s = sess.get("date"), sess.get("start_time")
    if not date_s or not time_s:
        return False
    try:
        # Naive local, compared against local now. The database does the same
        # arithmetic with `AT TIME ZONE 'Australia/Melbourne'`.
        starts = datetime.fromisoformat(f"{date_s}T{time_s}:00")
    except ValueError:
        return False
    return datetime.now() >= starts


def release_abandoned_holds(client, accounts):
    """Cancel authorisations nobody is going to capture.

    ══ WHY THIS FILE OWNS IT ═════════════════════════════════════════════
    The sweep above finds `status = 'pending'` rows. A player promoted off the
    waitlist is `approved`, so a cancelled session left their money reserved
    until Stripe dropped the hold about a week later — outside every existing
    check.

    `holds_to_release()` is how those reach this sweep. It also finds the
    ORPHAN case a participant-keyed query structurally could not: an open
    attempt whose player has rejoined and now points at a different hold.

    ══ A "HOLD" MAY TURN OUT TO BE A PAYMENT ═════════════════════════════
    The far end of the capture race: the waitlist worker captured the money
    and died before recording it, and the session was cancelled in between.

    Cancelling that intent fails. Clearing the flag and moving on would be a
    captured payment with nothing in the database recording it — the same
    discarded fact the capture worker exists to prevent, in a different place.

    So a refusal is not a conclusion. The intent is RETRIEVED and its status
    read, and `reconcile_captured_hold` routes a succeeded one back through
    the ordinary settlement path — which records the payment and owes it back.
    One success path, not two.
    """
    rows = client.post(f"{SUPABASE_URL}/rest/v1/rpc/holds_to_release",
                       headers=HEADERS, json={})
    if rows.status_code >= 300:
        log(f"could not read abandoned holds — {rows.text[:80]}")
        return 0
    holds = rows.json() or []
    if not holds:
        return 0
    log(f"{len(holds)} abandoned hold(s)")

    done = 0
    for h in holds:
        intent = h["payment_intent_id"]
        host_id = h.get("host_id")

        if host_id not in accounts:
            p = client.get(
                f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{host_id}"
                "&select=stripe_account_id", headers=HEADERS,
            )
            prows = p.json() if p.status_code == 200 else []
            accounts[host_id] = prows[0].get("stripe_account_id") if prows else None
        acct = accounts[host_id]
        if not acct:
            log(f"SKIP {intent} — host has no Stripe account")
            continue

        if DRY:
            log(f"WOULD RELEASE {intent}")
            done += 1
            continue

        try:
            stripe.PaymentIntent.cancel(intent, stripe_account=acct)

        # ORDER MATTERS. These are subclasses of StripeError; below the general
        # handler they would be unreachable, and an outage would be treated as
        # a conclusion about the hold.
        except (stripe.error.APIConnectionError, stripe.error.RateLimitError):
            # Unknown, not failed. Left open for the next run.
            log(f"DEFERRED {intent} — stripe unreachable")
            continue

        except stripe.error.StripeError as e:
            # THE INTENT IS THE EVIDENCE. A refusal means Stripe would not
            # cancel it in its current state; only retrieving it says which.
            try:
                pi = stripe.PaymentIntent.retrieve(intent, stripe_account=acct)
            except stripe.error.StripeError:
                log(f"FAILED {intent} — {str(e)[:70]}")
                continue

            if pi.status == "succeeded" and (pi.amount_received or 0) > 0:
                # Captured, and never recorded. Settle it as the success it is;
                # the database decides whether that reaches the roster or
                # becomes a refund.
                #
                # AND THE RESULT IS CHECKED. Firing this and moving on would be
                # followed by `mark_hold_released`, closing the attempt as
                # RELEASED while Stripe held the money — the discarded
                # financial fact, arrived at through the recovery path. A
                # reconciliation that did not land leaves the attempt open, so
                # the next run finds it again.
                r = client.post(
                    f"{SUPABASE_URL}/rest/v1/rpc/reconcile_captured_hold",
                    headers=HEADERS,
                    json={"p_intent": intent, "p_amount": pi.amount_received / 100},
                )
                if r.status_code >= 300:
                    log(f"DEFERRED {intent} — captured, but reconciliation "
                        f"failed: {r.text[:70]}")
                    continue
                log(f"RECONCILED {intent} — was captured, refund now owed")
                done += 1
                continue

            if pi.status not in ("canceled", "cancelled"):
                # Neither cancellable nor captured — leave it open so the next
                # run looks again rather than forgetting about it.
                log(f"DEFERRED {intent} — intent is {pi.status}")
                continue
            # Already cancelled: nothing to do but record it.

        # Checked for the same reason: an unrecorded release leaves the attempt
        # open, which is harmless — the next run cancels an already cancelled
        # intent and records it then. Claiming it while the write failed is
        # what is not.
        r = client.post(f"{SUPABASE_URL}/rest/v1/rpc/mark_hold_released",
                        headers=HEADERS, json={"p_intent": intent})
        if r.status_code >= 300:
            log(f"DEFERRED {intent} — released, but not recorded: {r.text[:70]}")
            continue
        log(f"RELEASED {intent}")
        done += 1

    return done


def main():
    if not (SUPABASE_URL and SUPABASE_KEY and STRIPE_SECRET_KEY):
        log("ERROR missing SUPABASE_URL, SUPABASE_SERVICE_KEY or STRIPE_SECRET_KEY")
        return 1
    stripe.api_key = STRIPE_SECRET_KEY

    cutoff = datetime.now(timezone.utc) - timedelta(days=HOLD_DAYS)

    with httpx.Client(timeout=30.0) as client:
        # Every live hold. Filtered in Python rather than in the query because
        # "started" needs the session's date and time together, which PostgREST
        # cannot express as a comparison.
        r = client.get(
            f"{SUPABASE_URL}/rest/v1/session_participants"
            "?status=eq.pending&paid=is.false&payment_intent_id=not.is.null"
            "&select=id,session_id,user_id,payment_intent_id,joined_at",
            headers=HEADERS,
        )
        r.raise_for_status()
        held = r.json()

        if not held:
            log("no held payments")
        else:
            log(f"{len(held)} held payment(s) to consider")

        # Sessions, one request rather than one per row.
        ids = sorted({h["session_id"] for h in held})
        sessions = {}
        if ids:
            in_list = ",".join(f'"{i}"' for i in ids)
            rs = client.get(
                f"{SUPABASE_URL}/rest/v1/created_sessions?id=in.({in_list})"
                "&select=id,title,date,start_time,user_id",
                headers=HEADERS,
            )
            rs.raise_for_status()
            sessions = {s["id"]: s for s in rs.json()}

        # Host Stripe accounts. A direct charge lives on the connected account,
        # so the cancel must name it.
        host_ids = sorted({s.get("user_id") for s in sessions.values() if s.get("user_id")})
        accounts = {}
        if host_ids:
            in_list = ",".join(f'"{h}"' for h in host_ids)
            rp = client.get(
                f"{SUPABASE_URL}/rest/v1/profiles?id=in.({in_list})"
                "&select=id,stripe_account_id",
                headers=HEADERS,
            )
            rp.raise_for_status()
            accounts = {p["id"]: p.get("stripe_account_id") for p in rp.json()}

        released = 0
        for h in held:
            sess = sessions.get(h["session_id"])
            if not sess:
                log(f"SKIP {h['payment_intent_id']} — session {h['session_id']} not found")
                continue

            started = session_started(sess)
            joined = datetime.fromisoformat(h["joined_at"].replace("Z", "+00:00"))
            stale = joined <= cutoff

            if not (started or stale):
                continue

            why = "session started" if started else f"held {HOLD_DAYS}+ days"
            account_id = accounts.get(sess.get("user_id"))
            if not account_id:
                log(f"SKIP {h['payment_intent_id']} — host has no Stripe account")
                continue

            if DRY:
                log(f"WOULD RELEASE {h['payment_intent_id']} "
                    f"({sess.get('title')}) — {why}")
                released += 1
                continue

            try:
                stripe.PaymentIntent.cancel(
                    h["payment_intent_id"], stripe_account=account_id
                )
            except stripe.error.StripeError as e:
                # ALREADY CANCELLED IS DONE, NOT FAILED. The hold is gone and
                # the money is back with the player; only the row is stale —
                # a decline whose status write did not land, or a hold Stripe
                # expired on its own. Retrying it hourly forever would never
                # succeed and would never stop.
                if "status of canceled" in str(e) or "already been canceled" in str(e):
                    log(f"ALREADY CANCELLED {h['payment_intent_id']} — recording it")
                else:
                    # Left `pending` deliberately, so the next run tries again.
                    log(f"FAILED cancel {h['payment_intent_id']} — {e}")
                    continue

            pr = client.patch(
                f"{SUPABASE_URL}/rest/v1/session_participants?id=eq.{h['id']}",
                headers=HEADERS,
                json={"status": "rejected"},
            )
            if pr.status_code >= 300:
                # The hold is released and the money is back with the player,
                # which is the part that matters. The row is wrong and says so.
                log(f"CANCELLED but status not updated {h['payment_intent_id']} — {pr.text}")
                continue

            log(f"RELEASED {h['payment_intent_id']} ({sess.get('title')}) — {why}")
            released += 1

        # ── reported, not acted on ────────────────────────────────────────
        ra = client.get(
            f"{SUPABASE_URL}/rest/v1/session_participants"
            "?status=eq.approved&paid=is.false&payment_intent_id=not.is.null"
            "&select=id,session_id,payment_intent_id",
            headers=HEADERS,
        )
        if ra.status_code < 300:
            owed = ra.json()
            for o in owed:
                log(f"UNCAPTURED APPROVAL {o['payment_intent_id']} "
                    f"session {o['session_id']} — someone is in and nobody was charged")
            if owed:
                log(f"{len(owed)} approved participant(s) with money never taken")

        # ── holds the sweep above cannot see ──────────────────────────────
        # `+=`, not `=`. The two sweeps release different things and the total
        # is both — assigning here would silently discard the count above.
        released += release_abandoned_holds(client, accounts)

        log(f"done — {released} released{' (dry run)' if DRY else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
