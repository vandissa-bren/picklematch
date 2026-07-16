"""
fetch_announcements_gmail.py — Poll Gmail for PBP venue announcements.
Reads emails from picklematchannouncements@gmail.com sent by *@playbypoint.com
and stores them in Supabase announcements table.
Run via cron every 15 minutes.
"""
import json
import os
import re
import base64
import httpx
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://stwohmddmdwttasbyblt.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN0d29obWRkbWR3dHRhc2J5Ymx0Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3ODcyNDc5MywiZXhwIjoyMDk0MzAwNzkzfQ.zrsXJVxX4OZv0Eb5qycQF3_33NFyAFJfPlvK_xCzi-E")
GMAIL_TOKEN_PATH = "/app/.gmail_token.json"

VENUE_NAME_MAP = {
    "nplpickleball": "The Jar | South Melbourne",
    "thejarHQ": "The Jar | Maidstone",
    "easternindoorpickleballclub": "Eastern Indoor Pickleball Club",
    "pickleholic": "PICKLEHOLIC",
    "statepickleballcentre": "State Pickleball Centre",
    "melbournepickleclub": "Melbourne Pickle Club",
    "picklehaus": "Pickle Haus",
    "leveluppickleballknoxcity": "Level Up Pickleball Knox City",
    "theroompickleball": "The Room Pickleball",
    "therealdill": "The Real Dill",
    "pickleplex": "PicklePlex",
    "dinkndrivepickleballclub": "Dink & Drive Pickleball Club",
    "swingandserve": "Swing & Serve",
    "pickle-playground": "Pickle Playground",
    "therallypickleball": "The Rally Pickleball",
    "runwaypickleball": "Runway Pickleball",
    "pickleballpowerhouse": "Pickleball Powerhouse",
    "picklezone": "Picklezone",
    "rayapickleballclub": "Raya Pickleball Club",
    "pickle4real": "PICKLE4REAL",
    "sportswellpickleballpalace": "SportsWell | Pickleball Palace",
    "pickleballpalace": "SportsWell | Pickleball Palace",
    "sportswell": "SportsWell | Pickleball Palace",
}

FACILITY_ID_MAP = {
    "The Jar | South Melbourne": 597,
    "The Jar | Maidstone": 1883,
    "Eastern Indoor Pickleball Club": 1009,
    "PICKLEHOLIC": 1379,
    "State Pickleball Centre": 1355,
    "Melbourne Pickle Club": 1383,
    "Pickle Haus": 1485,
    "Level Up Pickleball Knox City": 755,
    "The Room Pickleball": 1584,
    "The Real Dill": 1461,
    "PicklePlex": 1532,
    "Dink & Drive Pickleball Club": 1557,
    "Swing & Serve": 1119,
    "Pickle Playground": 1487,
    "The Rally Pickleball": 1664,
    "Runway Pickleball": 1714,
    "Pickleball Powerhouse": 1733,
    "Picklezone": 1696,
    "Raya Pickleball Club": 1770,
    "PICKLE4REAL": 1783,
    "SportsWell | Pickleball Palace": 885,
}

SKIP_SUBJECTS = [
    "welcome to",
    "you are now subscribed",
    "thanks for subscribing",
    "subscription confirmed",
]


def get_access_token():
    with open(GMAIL_TOKEN_PATH) as f:
        token_data = json.load(f)
    resp = httpx.post(token_data["token_uri"], data={
        "client_id": token_data["client_id"],
        "client_secret": token_data["client_secret"],
        "refresh_token": token_data["refresh_token"],
        "grant_type": "refresh_token",
    })
    return resp.json()["access_token"]


def list_unread_pbp_emails(access_token):
    resp = httpx.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        params={"q": "from:@playbypoint.com is:unread", "maxResults": 50},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return resp.json().get("messages", [])


def get_email(access_token, msg_id):
    resp = httpx.get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
        params={"format": "raw"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    data = resp.json()
    raw = base64.urlsafe_b64decode(data["raw"] + "==")
    msg = message_from_bytes(raw)
    return msg, data.get("internalDate")


def decode_str(s):
    parts = decode_header(s)
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="replace")
        else:
            result += part
    return result


def strip_html(html):
    html = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.S | re.I)
    html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.S | re.I)
    html = re.sub(r'<(br|p|div|tr|li|h[1-6])[^>]*>', '\n', html, flags=re.I)
    html = re.sub(r'<[^>]+>', '', html)
    html = html.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<')
    html = html.replace('&gt;', '>').replace('&#39;', "'").replace('&quot;', '"')
    html = re.sub(r'\n\s*\n+', '\n\n', html)
    html = re.sub(r'[ \t]+', ' ', html)
    return html.strip()


def extract_body(msg):
    body = ""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace").strip()
            elif ct == "text/html" and not html_body:
                payload = part.get_payload(decode=True)
                if payload:
                    html_body = payload.decode("utf-8", errors="replace")
    else:
        ct = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            if ct == "text/html":
                html_body = payload.decode("utf-8", errors="replace")
            else:
                body = payload.decode("utf-8", errors="replace").strip()

    if not body and html_body:
        body = strip_html(html_body)

    body = re.sub(r"You are receiving this email.*$", "", body, flags=re.S | re.I).strip()
    body = re.sub(r"To unsubscribe.*$", "", body, flags=re.S | re.I).strip()
    return body[:2000]


def extract_venue_name(sender, subject, body):
    sender_match = re.search(r"([\w\-]+)@playbypoint\.com", sender)
    if sender_match:
        slug = sender_match.group(1).lower().replace("-", "").replace("_", "")
        for key, name in VENUE_NAME_MAP.items():
            if key.lower().replace("-", "").replace("_", "") == slug:
                return name
    first_line = body.strip().split("\n")[0].strip()
    for name in FACILITY_ID_MAP:
        if name.lower() in first_line.lower() or first_line.lower() in name.lower():
            return name
    for name in FACILITY_ID_MAP:
        if name.lower() in subject.lower():
            return name
    # Try partial keyword match on subject
    subject_lower = subject.lower()
    keyword_map = {
        "pickleball palace": "SportsWell | Pickleball Palace",
        "sportswell": "SportsWell | Pickleball Palace",
        "pascoe vale": "SportsWell | Pickleball Palace",
        "the jar": "The Jar | South Melbourne",
        "maidstone": "The Jar | Maidstone",
        "eastern indoor": "Eastern Indoor Pickleball Club",
        "pickleholic": "PICKLEHOLIC",
        "state pickleball": "State Pickleball Centre",
        "melbourne pickle": "Melbourne Pickle Club",
        "pickle haus": "Pickle Haus",
        "level up": "Level Up Pickleball Knox City",
        "the room": "The Room Pickleball",
        "real dill": "The Real Dill",
        "pickleplex": "PicklePlex",
        "dink": "Dink & Drive Pickleball Club",
        "swing": "Swing & Serve",
        "pickle playground": "Pickle Playground",
        "the rally": "The Rally Pickleball",
        "runway": "Runway Pickleball",
        "powerhouse": "Pickleball Powerhouse",
        "picklezone": "Picklezone",
        "raya": "Raya Pickleball Club",
        "pickle4real": "PICKLE4REAL",
    }
    for keyword, name in keyword_map.items():
        if keyword in subject_lower:
            return name
    return first_line[:100] if first_line else "Unknown Venue"


def mark_as_read(access_token, msg_id):
    httpx.post(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/modify",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"removeLabelIds": ["UNREAD"]},
    )


def store_announcement(venue_name, facility_id, subject, body, sent_at):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    announcement_id = abs(hash(f"{facility_id}-{subject}-{sent_at}")) % (10**9)
    record = {
        "id": f"gmail-{announcement_id}",
        "announcement_id": announcement_id,
        "facility_id": facility_id,
        "facility_name": venue_name,
        "title": subject,
        "body_text": body,
        "body_html": body,
        "date_str": f"{venue_name} - {sent_at[:10]}",
        "url": "",
        "fetched_at": sent_at,
    }
    resp = httpx.post(
        f"{SUPABASE_URL}/rest/v1/announcements",
        headers=headers,
        json=record,
    )
    if resp.status_code not in (200, 201):
        print(f"    Supabase error: {resp.status_code} {resp.text[:200]}")
    return resp.status_code in (200, 201)


def extract_codes_from_text(body, venue_name, facility_id, announcement_id, sent_at):
    """Extract promo codes from announcement body and store in Supabase."""
    import re as _re
    found = set()
    # Match: use code XXXX, code: XXXX, promo code XXXX, enter XXXX
    for m in _re.finditer(r"(?:use|enter|apply|promo|coupon)\s+code[:\s]+([A-Z0-9]{4,20})", body, _re.IGNORECASE):
        code = m.group(1).upper()
        if not code.isdigit():
            found.add(code)
    # Match quoted codes like "BOOSTER" or 'PINKPALACE'
    for m in _re.finditer(r'["\']([A-Z][A-Z0-9]{3,19})["\']', body):
        code = m.group(1).upper()
        if not code.isdigit():
            found.add(code)
    if not found:
        return
    # Try to detect expiry
    expiry = None
    exp_m = _re.search(r"(?:valid until|expires?)[:\s]+(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", body, _re.IGNORECASE)
    if exp_m:
        expiry = exp_m.group(1)
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=ignore-duplicates",
    }
    for code in found:
        record = {
            "code": code,
            "facility_id": facility_id,
            "facility_name": venue_name,
            "description": body[:200],
            "source": "announcement",
            "expires_at": expiry,
            "announcement_id": announcement_id,
            "discovered_at": sent_at,
        }
        try:
            httpx.post(f"{SUPABASE_URL}/rest/v1/promo_codes", headers=headers, json=record)
            print(f"    Promo code extracted: {code}")
        except Exception as e:
            print(f"    Failed to store code {code}: {e}")


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Polling Gmail for PBP announcements...")
    access_token = get_access_token()
    messages = list_unread_pbp_emails(access_token)
    print(f"Found {len(messages)} unread PBP emails")

    stored = 0
    for m in messages:
        msg_id = m["id"]
        try:
            msg, internal_date = get_email(access_token, msg_id)
            sender = msg.get("From", "")
            subject = decode_str(msg.get("Subject", "No subject"))
            body = extract_body(msg)
            sent_at = datetime.fromtimestamp(
                int(internal_date) / 1000, tz=timezone.utc
            ).isoformat() if internal_date else datetime.now(timezone.utc).isoformat()

            venue_name = extract_venue_name(sender, subject, body)
            facility_id = FACILITY_ID_MAP.get(venue_name)

            print(f"  -> {venue_name} (fid:{facility_id}): {subject[:60]}")

            if any(s in subject.lower() for s in SKIP_SUBJECTS):
                print(f"    - Skipping welcome/subscription email")
                mark_as_read(access_token, msg_id)
                continue

            if facility_id:
                ok = store_announcement(venue_name, facility_id, subject, body, sent_at)
                if ok:
                    stored += 1
                    mark_as_read(access_token, msg_id)
                    print(f"    + Stored and marked as read")
                    announcement_id = abs(hash(f"{facility_id}-{subject}-{sent_at}")) % (10**9)
                    extract_codes_from_text(body, venue_name, facility_id, announcement_id, sent_at)
                else:
                    print(f"    x Failed to store")
            else:
                print(f"    x Could not match to known venue -- skipping")
                mark_as_read(access_token, msg_id)

        except Exception as e:
            print(f"  Error processing {msg_id}: {e}")

    print(f"Done. {stored} new announcements stored.")


if __name__ == "__main__":
    main()
