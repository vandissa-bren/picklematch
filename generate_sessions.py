#!/usr/bin/env python3
"""
Generate sessions from active recurring templates.
Run via cron or GitHub Actions weekly.
"""
import os
import httpx
import uuid
from datetime import datetime, timedelta, date
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://stwohmddmdwttasbyblt.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN0d29obWRkbWR3dHRhc2J5Ymx0Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3ODcyNDc5MywiZXhwIjoyMDk0MzAwNzkzfQ.zrsXJVxX4OZv0Eb5qycQF3_33NFyAFJfPlvK_xCzi-E")

HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

def next_weekday(from_date: date, day_of_week: int) -> date:
    """Get next occurrence of day_of_week (0=Sun, 1=Mon ... 6=Sat) from from_date."""
    # Python weekday: 0=Mon, 6=Sun — convert
    target = (day_of_week + 6) % 7  # convert to Python weekday
    days_ahead = target - from_date.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return from_date + timedelta(days=days_ahead)

def generate_invite_token() -> str:
    return uuid.uuid4().hex[:12].upper()

def main():
    with httpx.Client(timeout=30.0) as client:
        # Fetch all active templates
        resp = client.get(
            f"{SUPABASE_URL}/rest/v1/session_templates?active=eq.true&select=*",
            headers=HEADERS,
        )
        templates = resp.json()
        print(f"Found {len(templates)} active templates")

        today = date.today()

        for t in templates:
            weeks_ahead = t.get("generate_weeks_ahead", 2)
            end_date = today + timedelta(weeks=weeks_ahead)
            day_of_week = t["day_of_week"]
            recurrence = t.get("recurrence", "weekly")
            interval_weeks = 2 if recurrence == "fortnightly" else 1

            # Find all target dates from today to end_date
            target_date = next_weekday(today, day_of_week)
            dates_to_generate = []
            while target_date <= end_date:
                dates_to_generate.append(target_date)
                target_date += timedelta(weeks=interval_weeks)

            print(f"Template {t['id']} ({t.get('title') or t['type']}) — {len(dates_to_generate)} dates to check")

            for session_date in dates_to_generate:
                date_str = session_date.isoformat()

                # Check if session already exists for this template + date
                existing = client.get(
                    f"{SUPABASE_URL}/rest/v1/created_sessions?template_id=eq.{t['id']}&date=eq.{date_str}&select=id",
                    headers=HEADERS,
                ).json()

                if existing:
                    print(f"  ✓ {date_str} already exists, skipping")
                    continue

                # Create the session
                session = {
                    "id": str(uuid.uuid4()),
                    "user_id": t["host_id"],
                    "template_id": t["id"],
                    "invite_token": generate_invite_token(),
                    "title": t.get("title") or t["type"],
                    "type": t["type"],
                    "date": date_str,
                    "start_time": t["start_time"],
                    "end_time": t["end_time"],
                    "skill_level": t.get("skill_level", "All levels"),
                    "spots_available": t.get("max_spots", 16),
                    "max_spots": t.get("max_spots", 16),
                    "price": t.get("price", "Free"),
                    "status": "Available",
                    "description": t.get("description", ""),
                    "venue_id": t["venue_id"],
                    "courts": t.get("courts") or [],
                    "visibility": t.get("visibility", "public"),
                    "require_approval": t.get("require_approval", False),
                    "dupr_min": t.get("dupr_min"),
                    "dupr_max": t.get("dupr_max"),
                }

                create_resp = client.post(
                    f"{SUPABASE_URL}/rest/v1/created_sessions",
                    headers={**HEADERS, "Prefer": "return=minimal"},
                    json=session,
                )

                if create_resp.status_code in (200, 201, 204):
                    print(f"  ✓ Created session for {date_str}")
                    # Notify subscribers
                    subs = client.get(
                        f"{SUPABASE_URL}/rest/v1/host_subscriptions?host_id=eq.{t['host_id']}&select=subscriber_id",
                        headers=HEADERS,
                    ).json()
                    host_profile = client.get(
                        f"{SUPABASE_URL}/rest/v1/host_profiles?user_id=eq.{t['host_id']}&select=display_name,username",
                        headers=HEADERS,
                    ).json()
                    host_name = host_profile[0].get("display_name") or host_profile[0].get("username") if host_profile else "A host"
                    if subs:
                        notifications = [{
                            "user_id": s["subscriber_id"],
                            "type": "new_session",
                            "message": f"{host_name} just posted a new session — {session['title']} on {session_date.strftime('%a %-d %b')}",
                            "link": f"/join/{session['invite_token']}",
                        } for s in subs]
                        client.post(
                            f"{SUPABASE_URL}/rest/v1/notifications",
                            headers={**HEADERS, "Prefer": "return=minimal"},
                            json=notifications,
                        )
                        print(f"  ✓ Notified {len(subs)} subscribers")
                else:
                    print(f"  ✗ Failed to create session for {date_str}: {create_resp.text}")

            # Update last_generated_at
            client.patch(
                f"{SUPABASE_URL}/rest/v1/session_templates?id=eq.{t['id']}",
                headers=HEADERS,
                json={"last_generated_at": datetime.utcnow().isoformat()},
            )

        print("Done.")

if __name__ == "__main__":
    main()
