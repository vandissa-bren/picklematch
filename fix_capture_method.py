#!/usr/bin/env python3
"""
Adds `purpose` to /api/stripe/payment_intent and derives capture_method from it.

    cd /app && /app/venv/bin/python3 fix_capture_method.py

It asserts before writing: if api_server.py does not match what is expected,
it changes nothing and says so.

══ THE BUG ═══════════════════════════════════════════════════════════════
`capture_method` was chosen from `require_approval`, on the reasoning that a
session needing approval must hold rather than charge. True, and it stopped
being sufficient the moment a SECOND path took holds.

A waitlist join on an approval-off session therefore got `automatic`, so
confirming the payment sheet CHARGED the player — while the client wrote
`paid: false`, because it knew the payment was meant to be a hold.

Observed 17 Aug in /var/log/waitlist_captures.log:

    ALREADY CAPTURED pi_3U5LP4K6VcCD3jML0L6r79hm — recorded

The worker recovered it by retrieving the intent and recording the truth. That
is the reconciliation path working, and it is only needed because the intent
was created wrong.

══ THE FIX ═══════════════════════════════════════════════════════════════
The client sends what the payment is FOR. The server decides what that means.

    join, approval off        automatic   charge on confirmation
    join, approval on         manual      hold, captured on approval
    waitlist, either way      manual      hold, captured on promotion

`require_approval` stops being a proxy for "this is a hold" and becomes one of
two inputs to the question.

══ THE CLIENT CANNOT NAME THE CAPTURE METHOD ═════════════════════════════
`purpose` is a purpose, not a `capture_method`. A client that could send
`capture_method: 'manual'` directly could decide not to be charged, and an
unrecognised purpose falls through to the session's own settings rather than
to whatever was asked for.
"""
import pathlib
import sys

p = pathlib.Path("/app/api_server.py")
s = p.read_text()

# ── 1 · the request model accepts a purpose ───────────────────────────────
old = '''class PaymentIntentRequest(BaseModel):
    user_id: str
    session_id: str
    host_user_id: str
    amount: int  # in cents
    currency: str = "aud"
    description: str = "PickleMatch session"'''

new = '''class PaymentIntentRequest(BaseModel):
    user_id: str
    session_id: str
    host_user_id: str
    amount: int  # in cents
    currency: str = "aud"
    description: str = "PickleMatch session"
    # WHAT THE PAYMENT IS FOR, not how to take it. The server derives
    # capture_method from this and the session's own settings; a client that
    # could name the capture method could decide not to be charged.
    # Defaults to "join" so an older client keeps its existing behaviour.
    purpose: str = "join"'''

if old not in s:
    print("ABORTED — PaymentIntentRequest is not as expected. Nothing changed.")
    sys.exit(1)
s = s.replace(old, new)

# ── 2 · derive capture_method from purpose AND the session ────────────────
old2 = '''    # AUTHORISE NOW, CAPTURE ON APPROVAL. A session the host must agree to has
    # not been agreed to yet, so the money must not move yet: the card is held
    # and captured when they approve, or the hold is cancelled when they
    # decline and nothing is ever taken. A player declined after being charged
    # has to be refunded, which is the failure this removes.
    #
    # Sessions anyone can join capture immediately, exactly as before.
    capture_method = "manual" if requires_approval else "automatic"'''

new2 = '''    # ══ HOLD OR CHARGE ═══════════════════════════════════════════════════
    # AUTHORISE NOW, CAPTURE LATER wherever the place is not yet the player's:
    # the card is held, and captured when it becomes theirs — or cancelled,
    # and nothing is ever taken. A player charged for a place they never got
    # has to be refunded, which is the failure this avoids.
    #
    # TWO PATHS NEED A HOLD, NOT ONE. This read `requires_approval` alone,
    # which was correct while approval was the only reason to wait. A WAITLIST
    # join also waits — for a place to open — and on an approval-off session it
    # was therefore CHARGED at once, while the client recorded it as a hold.
    # The worker recovered those with "ALREADY CAPTURED"; the intent should not
    # have needed recovering.
    #
    #     join, approval off      automatic
    #     join, approval on       manual — captured when the host approves
    #     waitlist, either way    manual — captured when a place opens
    is_waitlist = (req.purpose or "join").strip().lower() == "waitlist"
    capture_method = "manual" if (requires_approval or is_waitlist) else "automatic"'''

if old2 not in s:
    print("ABORTED — the capture_method block is not as expected. Nothing changed.")
    sys.exit(1)
s = s.replace(old2, new2)

p.write_text(s)
print("api_server.py updated")
print("  purpose accepted on PaymentIntentRequest")
print("  capture_method = manual when require_approval OR purpose == waitlist")
