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
no decision to preserve: if the cancel fails, leaving the row `pending` means
the next run tries again, whereas marking it rejected would strand the hold
with nothing left to notice it.

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

        log(f"done — {released} released{' (dry run)' if DRY else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
