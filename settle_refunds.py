"""
settle_refunds.py — pay what the ledger says is owed.

The database decides what is owed and why; this moves the money. Nothing here
makes a policy decision — if you find yourself computing an amount or deciding
whether a refund applies, something is in the wrong place.

    refunds_due()                       what is owed, to which intent, on
                                        whose connected account
    settle_refund(id, ok, failure)      mark it done, and tell the player
    refund_needs_funds(id)              tell the host their balance is short

THREE OUTCOMES, and they are not the same:

    refunded    money returned, player notified, row closed

    needs funds the connected account is empty. Left `due` so the next run
                tries again — this resolves on its own when the host takes
                another booking — and the host is told ONCE rather than hourly

    failed      disputed, already refunded, no such intent, account
                restricted. Terminal: retrying either loops forever or
                double-refunds the moment the cause clears. Needs a person.

    cd /app && /app/venv/bin/python3 settle_refunds.py --dry-run
    cd /app && /app/venv/bin/python3 settle_refunds.py
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
# No hardcoded fallback. A service key baked into a file is that key published
# the moment the file reaches a repo — which is exactly how one leaked here.
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

DRY = "--dry-run" in sys.argv

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def rpc(client, name, payload=None):
    r = client.post(f"{SUPABASE_URL}/rest/v1/rpc/{name}",
                    headers=HEADERS, json=payload or {})
    r.raise_for_status()
    return r.json() if r.text else None


def main():
    if not (SUPABASE_URL and SUPABASE_KEY and STRIPE_SECRET_KEY):
        log("ERROR missing SUPABASE_URL, SUPABASE_SERVICE_KEY or STRIPE_SECRET_KEY")
        return 1
    stripe.api_key = STRIPE_SECRET_KEY

    with httpx.Client(timeout=30.0) as client:
        due = rpc(client, "refunds_due") or []
        if not due:
            log("nothing owed")
            return 0
        log(f"{len(due)} refund(s) owed")

        # Host Stripe accounts. A direct charge lives on the connected account,
        # so the refund must be issued against it — not the platform. The
        # legacy manual refund endpoint got this wrong and never worked.
        host_ids = sorted({d["host_id"] for d in due if d.get("host_id")})
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

        settled = short = failed = 0

        for d in due:
            rid = d["refund_id"]
            intent = d["payment_intent_id"]
            amount = float(d["refund_amount"] or 0)
            label = f"{intent} ${amount:.2f} ({d.get('reason')})"

            if amount <= 0:
                # `refunds_zero_is_not_due` should make this unreachable.
                log(f"SKIP {label} — nothing to refund")
                continue

            account_id = accounts.get(d.get("host_id"))
            if not account_id:
                log(f"SKIP {label} — host has no Stripe account")
                continue

            if DRY:
                log(f"WOULD REFUND {label} on {account_id}")
                settled += 1
                continue

            try:
                # DOLLARS IN THE LEDGER, CENTS AT STRIPE. Getting this wrong by
                # a factor of 100 is the likeliest bug here, in either
                # direction, and neither direction is recoverable quietly.
                stripe.Refund.create(
                    payment_intent=intent,
                    amount=int(round(amount * 100)),
                    stripe_account=account_id,
                )
            except stripe.error.StripeError as e:
                code = getattr(e, "code", "") or ""
                detail = str(e)[:400]

                if code == "balance_insufficient":
                    # Not terminal: the host takes another booking and the
                    # money is there. Left `due`, host told once.
                    rpc(client, "refund_needs_funds", {"p_refund_id": rid})
                    log(f"NEEDS FUNDS {label} — left due, will retry"
                        + ("" if d.get("host_notified") else " (host notified)"))
                    short += 1
                    continue

                rpc(client, "settle_refund",
                    {"p_refund_id": rid, "p_ok": False, "p_failure": detail})
                log(f"FAILED {label} — {detail}")
                failed += 1
                continue

            rpc(client, "settle_refund", {"p_refund_id": rid, "p_ok": True})
            log(f"REFUNDED {label}")
            settled += 1

        log(f"done — {settled} refunded, {short} awaiting funds, {failed} failed"
            + (" (dry run)" if DRY else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
