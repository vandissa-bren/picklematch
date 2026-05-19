#!/usr/bin/env python3
"""
extract_sportlogic.py  —  SportLogic court availability + pricing scraper.

Scrapes public booking calendars from SportLogic-powered pickleball venues.
Availability is public (no login). Pricing requires a login session.

Pricing flow (HAR-verified 2026-05-15):
  1. GET /secure/customer/booking/v2/public/venue/{id}   → JSESSIONID cookie
  2. GET /secure/customer/booking/v2/book?rid=C1&date=YYYYMMDD&time=HH:MM:SS
     → initializes BookingFormV2 session attribute
  3. POST /secure/customer/booking/v2/form  (endTime=HH:MM:SS, name, email, ...)
     → HTML contains: <span style="font-size: 28px;">Fee Due: $30.00 AUD</span>

Login flow (reCAPTCHA protected — uses Playwright):
  POST /secure/customer/login  (userName, userPassword, g-recaptcha-response)
  → redirects to home, JSESSIONID becomes authenticated

Session cache: ~/.cache/extract_sportlogic/{subdomain}_session.json
  Stores: {jsessionid, email, cached_at}

Usage:
  python extract_sportlogic.py venues
  python extract_sportlogic.py search picklepark
  python extract_sportlogic.py search picklepark --days 7 --pricing
  python extract_sportlogic.py search pickleplay --from 18:00 --to 22:00 --pricing
  python extract_sportlogic.py find picklepark,pickleplay --date 2026-05-20
  python extract_sportlogic.py login picklepark   # login + cache session
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

# ─── Cache dir ─────────────────────────────────────────────────
CACHE_DIR = Path.home() / ".cache" / "extract_sportlogic"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ─── Known Venues ──────────────────────────────────────────────
VENUES: dict[str, dict] = {
    "picklepark": {
        "name": "Pickle Park Caulfield",
        "base_url": "https://picklepark.sportlogic.net.au",
        "venue_id": 1,
    },
    "pickleplay": {
        "name": "Pickle Play",
        "base_url": "https://pickleplay.sportlogic.net.au",
        "venue_id": 1,
    },
}

# Regex to extract price from the form submission response.
# Matches: Fee Due: $30.00 AUD
PRICE_RE = re.compile(r'Fee Due:\s*\$?([\d,]+\.?\d*)\s*AUD', re.IGNORECASE)

# Court slot button pattern.
SLOT_RE = re.compile(
    r"id='available_(?P<court>\w+)_(?P<date>\d{8})_(?P<time>\d{4})'"
)
COURT_ID_RE = re.compile(r'^C\d+$')
COURT_NAME_RE = re.compile(r'<h3>(Court \d+)</h3>')
BOOKING_URL_RE = re.compile(
    r"checkResourceAvailability\("
    r"'(?P<time>\d{2}:\d{2}:\d{2})',"
    r"'(?P<court>\w+)',"
    r"'(?P<url>[^']+)',"
)
VENUE_NAME_RE = re.compile(r'<h[12][^>]*>(.*?)</h[12]>', re.DOTALL)

# ─── Session management ────────────────────────────────────────

def _session_path(venue_key: str) -> Path:
    return CACHE_DIR / f"{venue_key}_session.json"


def _load_session(venue_key: str) -> Optional[str]:
    """Load cached JSESSIONID for a venue. Returns None if not found."""
    p = _session_path(venue_key)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        email = d.get("email", "")
        current_email = os.getenv("SL_EMAIL", "")
        if current_email and email and current_email.lower() != email.lower():
            return None  # Email changed, invalidate.
        return d.get("jsessionid")
    except Exception:
        return None


def _save_session(venue_key: str, jsessionid: str, email: str = "") -> None:
    """Cache a JSESSIONID for a venue."""
    p = _session_path(venue_key)
    p.write_text(json.dumps({
        "jsessionid": jsessionid,
        "email": email,
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")


# ─── HTML parsing ──────────────────────────────────────────────

def parse_calendar_html(html: str) -> list[dict]:
    """Parse calendar widget HTML into slot dicts."""
    court_names: dict[str, str] = {}
    for m in COURT_NAME_RE.finditer(html):
        name = m.group(1)
        num = re.search(r'\d+', name)
        if num:
            court_names[f"C{num.group()}"] = name

    booking_urls: dict[tuple, str] = {}
    for m in BOOKING_URL_RE.finditer(html):
        key = (m.group("court"), m.group("time")[:5].replace(":", ""))
        booking_urls[key] = m.group("url")

    slots = []
    for m in SLOT_RE.finditer(html):
        court_id = m.group("court")
        slot_date = m.group("date")
        slot_time = m.group("time")
        if not COURT_ID_RE.match(court_id):
            continue

        hour = int(slot_time[:2])
        minute = int(slot_time[2:])
        time_str = f"{hour:02d}:{minute:02d}"
        time_api = f"{hour:02d}:{minute:02d}:00"
        court_name = court_names.get(court_id, court_id.replace("C", "Court "))
        formatted_date = f"{slot_date[:4]}-{slot_date[4:6]}-{slot_date[6:8]}"
        url_key = (court_id, slot_time)

        slots.append({
            "court_id": court_id,
            "court_name": court_name,
            "date": formatted_date,
            "date_api": slot_date,
            "time": time_str,
            "time_api": time_api,
            "hour": hour,
            "booking_url_path": booking_urls.get(url_key, ""),
            "price": None,
        })

    slots.sort(key=lambda s: (s["time"], s["court_id"]))
    return slots


def extract_price(html: str) -> Optional[dict]:
    """
    Extract pricing from the SportLogic form submission response.

    Returns dict:
      {
        "price": "$30.00",          # price you pay (member or standard)
        "full_price": "$40.00",     # non-member/rack rate (if saved amount shown)
        "saved": "$10.00",          # amount saved (if member discount)
        "is_member_price": True,    # whether this is a discounted member rate
        "label": "member price",    # pricing label from HTML
      }
    """
    result: dict = {
        "price": None,
        "full_price": None,
        "saved": None,
        "is_member_price": False,
        "label": "",
    }

    # Primary: "Fee Due: $30.00 AUD"
    m = re.search(r'Fee Due:\s*\$?([\d,]+\.?\d*)\s*AUD', html, re.IGNORECASE)
    if m:
        result["price"] = f"${float(m.group(1).replace(',', '')):.2f}"
    else:
        # Fallback dollar amount
        m2 = re.search(r'\$(\d+(?:\.\d{2})?)\s*AUD', html)
        if m2:
            result["price"] = f"${float(m2.group(1)):.2f}"

    # "Total saved $10.00" → member discount
    saved_m = re.search(r'Total saved\s*\$?([\d,]+\.?\d*)', html, re.IGNORECASE)
    if saved_m:
        saved_amt = float(saved_m.group(1).replace(',', ''))
        result["saved"] = f"${saved_amt:.2f}"
        result["is_member_price"] = True
        # Compute rack rate = price paid + saved
        if result["price"]:
            paid = float(result["price"].replace('$', ''))
            result["full_price"] = f"${paid + saved_amt:.2f}"

    # "[member price]" label in fee summary
    label_m = re.search(r'\[(.*?price.*?)\]', html, re.IGNORECASE)
    if label_m:
        result["label"] = label_m.group(1).strip()
        result["is_member_price"] = True

    return result if result["price"] else None


# ─── API Client ────────────────────────────────────────────────

class SportLogicClient:
    """
    Client for a single SportLogic venue.
    Handles session management, availability scraping, and pricing.
    """

    def __init__(self, venue_key: str, jsessionid: Optional[str] = None):
        if venue_key not in VENUES:
            raise ValueError(f"Unknown venue '{venue_key}'. Known: {', '.join(VENUES)}")

        self.venue_key = venue_key
        self.venue = VENUES[venue_key]
        self.base_url = self.venue["base_url"]
        self.venue_id = self.venue["venue_id"]
        self._jsessionid = jsessionid or _load_session(venue_key)
        self._session_ready = False
        self._name = os.getenv("SL_NAME", "Guest")
        self._email = os.getenv("SL_EMAIL", "guest@example.com")

        cookies = {}
        if self._jsessionid:
            cookies["JSESSIONID"] = self._jsessionid

        self._client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            cookies=cookies,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36"
                ),
            },
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self._client.aclose()

    async def _init_session(self) -> None:
        """Visit venue page to initialize server-side session."""
        if self._session_ready:
            return
        url = f"{self.base_url}/secure/customer/booking/v2/public/venue/{self.venue_id}"
        resp = await self._client.get(url, headers={"Accept": "text/html"})
        resp.raise_for_status()
        # Grab the new JSESSIONID if we didn't have one.
        if not self._jsessionid:
            jsid = resp.cookies.get("JSESSIONID")
            if jsid:
                self._jsessionid = jsid
        self._session_ready = True

    async def get_availability(self, target_date: date) -> list[dict]:
        """Fetch available court slots for a date."""
        await self._init_session()
        date_param = target_date.strftime("%Y%m%d")
        resp = await self._client.get(
            f"{self.base_url}/secure/customer/booking/v2/public/calendar-widget",
            params={"date": date_param},
            headers={
                "Accept": "*/*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/secure/customer/booking/v2/public/venue/{self.venue_id}",
            },
        )
        resp.raise_for_status()
        slots = parse_calendar_html(resp.text)
        for s in slots:
            s["venue_key"] = self.venue_key
            s["venue_name"] = self.venue["name"]
            if s["booking_url_path"] and not s["booking_url_path"].startswith("http"):
                s["booking_url"] = f"{self.base_url}{s['booking_url_path']}"
            else:
                s["booking_url"] = s["booking_url_path"]
        return slots

    async def get_price(self, court_id: str, slot_date: str, time_api: str) -> Optional[str]:
        """
        Fetch the price for a specific court/date/time slot.

        Requires an authenticated JSESSIONID (login first).
        Flow:
          1. GET /book?rid=...&date=...&time=... → init BookingFormV2
          2. POST /form (endTime = time + 1hr) → parse "Fee Due: $X AUD"

        Each call uses its own httpx client to avoid server-side session
        state cross-contamination between concurrent requests.
        """
        if not self._jsessionid:
            return None

        book_url = (
            f"{self.base_url}/secure/customer/booking/v2/book"
            f"?rid={court_id}&date={slot_date}&time={time_api}"
        )
        try:
            # Use a fresh client per price lookup to avoid session pollution.
            async with httpx.AsyncClient(
                timeout=20.0,
                follow_redirects=True,
                cookies={"JSESSIONID": self._jsessionid},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36"
                    ),
                },
            ) as client:
                # Step 1: GET booking page — initialises BookingFormV2 on server.
                r1 = await client.get(
                    book_url,
                    headers={
                        "Accept": "text/html",
                        "Referer": f"{self.base_url}/secure/customer/booking/v2/public/venue/{self.venue_id}",
                    },
                )
                if r1.status_code != 200:
                    return None

                # Carry over the updated JSESSIONID from step 1.
                updated_jsid = r1.cookies.get("JSESSIONID", self._jsessionid)

                # Parse available end times from the select dropdown.
                end_times = re.findall(r'<option value="(\d{2}:\d{2}:\d{2})">', r1.text)
                end_time = end_times[0] if end_times else _add_hour(time_api)

                # Step 2: POST /form using same client session — gets price summary.
                r2 = await client.post(
                    f"{self.base_url}/secure/customer/booking/v2/form",
                    data={
                        "endTime": end_time,
                        "name": self._name,
                        "email": self._email,
                        "disclaimerAgreed": "true",
                        "_disclaimerAgreed": "on",
                        "promoCode": "",
                        "creditVoucherNumber": "",
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": book_url,
                        "Accept": "text/html",
                    },
                    cookies={"JSESSIONID": updated_jsid},
                )
                if r2.status_code == 200:
                    return extract_price(r2.text)
        except Exception:
            pass
        return None

    async def enrich_with_prices(
        self,
        slots: list[dict],
        max_concurrent: int = 3,
        auto_refresh: bool = True,
    ) -> list[dict]:
        """
        Fetch prices for all slots sequentially.

        Must be sequential (not concurrent) — the SportLogic server stores
        booking form state in the session, so concurrent GET+POST pairs
        cross-contaminate each other's pricing context.
        """
        if not self._jsessionid:
            if auto_refresh:
                jsid = await _ensure_valid_session(self.venue_key)
                if jsid:
                    self._jsessionid = jsid
                    self._client.cookies.set("JSESSIONID", jsid)
                else:
                    return slots
            else:
                return slots

        refresh_attempted = False
        for slot in slots:
            price_data = await self.get_price(
                slot["court_id"], slot["date_api"], slot["time_api"],
            )
            if price_data is None and not refresh_attempted and auto_refresh:
                refresh_attempted = True
                jsid = await _ensure_valid_session(self.venue_key)
                if jsid:
                    self._jsessionid = jsid
                    self._client.cookies.set("JSESSIONID", jsid)
                    price_data = await self.get_price(
                        slot["court_id"], slot["date_api"], slot["time_api"],
                    )
            if isinstance(price_data, dict):
                slot["price"] = price_data.get("price")
                slot["full_price"] = price_data.get("full_price")
                slot["saved"] = price_data.get("saved")
                slot["is_member_price"] = price_data.get("is_member_price", False)
                slot["price_label"] = price_data.get("label", "")
            else:
                slot["price"] = None
                slot["full_price"] = None
                slot["saved"] = None
                slot["is_member_price"] = False
                slot["price_label"] = ""

        return slots


def _add_hour(time_api: str) -> str:
    """Add 1 hour to an HH:MM:SS string."""
    h, m, s = time_api.split(":")
    return f"{int(h)+1:02d}:{m}:{s}"


# ─── Playwright login ──────────────────────────────────────────

async def _playwright_login(venue_key: str, email: str, password: str) -> Optional[str]:
    """
    Log in via Playwright to bypass reCAPTCHA, return JSESSIONID.

    Uses a persistent browser profile so reCAPTCHA v3 scores well.
    reCAPTCHA v3 is invisible — no manual steps needed in a real browser.
    If it fails, waits up to 60s for manual intervention.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "Playwright not installed. Run: pip install playwright && "
            "playwright install chromium"
        )
        return None

    profile_dir = CACHE_DIR / f"{venue_key}_browser_profile"
    profile_dir.mkdir(exist_ok=True)
    base_url = VENUES[venue_key]["base_url"]
    jsessionid = None

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            ignore_https_errors=True,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Remove automation fingerprints.
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )

        print(f"  Opening login page for {VENUES[venue_key]['name']}…")
        await page.goto(f"{base_url}/secure/customer/user-login")
        await page.wait_for_load_state("domcontentloaded")

        # Fill credentials.
        await page.fill("input[name='userName']", email)
        await page.fill("input[name='userPassword']", password)

        # Small human-like delay before clicking.
        await asyncio.sleep(0.8)

        # Submit — reCAPTCHA v3 scores in background, no challenge shown.
        await page.click("button[type='submit'], input[type='submit']")

        try:
            # Wait for redirect away from login page (up to 20s normally).
            await page.wait_for_function(
                f"() => !window.location.href.includes('login')",
                timeout=20000,
            )
        except Exception:
            # Still on login page — reCAPTCHA may have challenged.
            # Wait up to 60s for manual resolution.
            print("  Still on login page — waiting up to 60s (solve captcha if prompted)…")
            try:
                await page.wait_for_function(
                    f"() => !window.location.href.includes('login')",
                    timeout=60000,
                )
            except Exception:
                print("  Login timed out.")
                await ctx.close()
                return None

        print(f"  Logged in. Capturing session cookie…")

        # Grab JSESSIONID from browser cookies.
        cookies = await ctx.cookies()
        for c in cookies:
            if c.get("name") == "JSESSIONID":
                jsessionid = c["value"]
                break

        await ctx.close()

    return jsessionid


async def _ensure_valid_session(venue_key: str) -> Optional[str]:
    """
    Ensure we have a valid JSESSIONID for a venue.

    Checks the cache first. If expired or missing, auto-logins via Playwright.
    Returns the JSESSIONID or None if login failed.
    """
    # Try cached session first.
    jsid = _load_session(venue_key)
    if jsid:
        # Quick probe to check if still valid.
        base_url = VENUES[venue_key]["base_url"]
        try:
            async with httpx.AsyncClient(
                cookies={"JSESSIONID": jsid},
                timeout=10.0,
                follow_redirects=False,
            ) as c:
                r = await c.get(f"{base_url}/secure/customer/home")
                if r.status_code in (200, 302) and "login" not in str(r.headers.get("location", "")):
                    return jsid  # Still valid.
        except Exception:
            pass
        # Session expired.
        print(f"  Session expired for {VENUES[venue_key]['name']} — re-logging in…")

    # Auto-login.
    email = os.getenv("SL_EMAIL", "")
    password = os.getenv("SL_PASSWORD", "")

    if not email or not password:
        print(
            f"  No credentials for {venue_key}. "
            f"Set SL_EMAIL and SL_PASSWORD in .env."
        )
        return None

    jsid = await _playwright_login(venue_key, email, password)
    if jsid:
        _save_session(venue_key, jsid, email)
        print(f"  Session refreshed for {VENUES[venue_key]['name']}.")

    return jsid


# ─── CLI ───────────────────────────────────────────────────────
cli = typer.Typer(
    name="extract_sportlogic",
    help="SportLogic court availability + pricing scraper.",
    add_completion=False,
)


@cli.command(name="venues")
def venues_cmd() -> None:
    """List known SportLogic venues."""
    console = Console()
    table = Table(title="Known SportLogic Venues")
    table.add_column("Key", style="bold")
    table.add_column("Name")
    table.add_column("URL")
    table.add_column("Session")

    for key, v in VENUES.items():
        cached = _load_session(key)
        session_str = "[green]✓ Cached[/green]" if cached else "[yellow]Not logged in[/yellow]"
        table.add_row(
            key,
            v["name"],
            f"{v['base_url']}/secure/customer/booking/v2/public/venue/{v['venue_id']}",
            session_str,
        )
    console.print(table)
    console.print(
        "\n[dim]Set SL_EMAIL and SL_PASSWORD in your .env file, "
        "then run 'login <venue>' to cache a pricing session.[/dim]"
    )


@cli.command(name="login")
def login_cmd(
    venue: str = typer.Argument(..., help="Venue key to log in to"),
    email: Optional[str] = typer.Option(None, "--email"),
    password: Optional[str] = typer.Option(None, "--password"),
    jsessionid: Optional[str] = typer.Option(
        None, "--jsessionid",
        help="Bypass login — provide JSESSIONID directly from browser cookies",
    ),
) -> None:
    """
    Log in to a SportLogic venue and cache the session for pricing.

    Credentials are read from .env (SL_EMAIL, SL_PASSWORD) or passed directly.
    Login uses Playwright to handle reCAPTCHA automatically.
    The cached session is reused for all subsequent --pricing calls, and
    automatically refreshed when it expires.

    Examples:
        python extract_sportlogic.py login picklepark
        python extract_sportlogic.py login pickleplay --email me@example.com --password secret
        python extract_sportlogic.py login picklepark --jsessionid ABCD1234  # manual fallback
    """
    console = Console()

    if venue not in VENUES:
        console.print(f"[red]Unknown venue '{venue}'.[/red]")
        raise typer.Exit(1)

    # Manual JSESSIONID override (escape hatch).
    if jsessionid:
        em = email or os.getenv("SL_EMAIL", "")
        _save_session(venue, jsessionid, em)
        console.print(f"[green]✓ JSESSIONID cached for {VENUES[venue]['name']}[/green]")
        return

    # Resolve credentials.
    em = email or os.getenv("SL_EMAIL", "")
    pw = password or os.getenv("SL_PASSWORD", "")

    if not em or not pw:
        console.print(
            "[red]No credentials found.[/red]\n\n"
            "Add to your .env file:\n"
            "  [bold]SL_EMAIL=your@email.com[/bold]\n"
            "  [bold]SL_PASSWORD=yourpassword[/bold]\n"
            "  [bold]SL_NAME=Your Name[/bold]\n\n"
            "Or pass directly:\n"
            "  python extract_sportlogic.py login picklepark "
            "--email me@example.com --password secret"
        )
        raise typer.Exit(1)

    console.print(f"\nLogging in to [bold]{VENUES[venue]['name']}[/bold] as {em}…")

    async def _do_login():
        jsid = await _playwright_login(venue, em, pw)
        if jsid:
            _save_session(venue, jsid, em)
        return jsid

    jsid = asyncio.run(_do_login())

    if jsid:
        console.print(f"[green]✓ Logged in and session cached for {VENUES[venue]['name']}[/green]")
        console.print(
            f"[dim]Session cached at: {_session_path(venue)}\n"
            f"Will auto-refresh when expired.[/dim]"
        )
    else:
        console.print("[red]✗ Login failed.[/red]")
        raise typer.Exit(1)


@cli.command(name="search")
def search_cmd(
    venue: str = typer.Argument(..., help=f"Venue key: {', '.join(VENUES)}"),
    days: int = typer.Option(1, "--days", "-d"),
    from_time: Optional[str] = typer.Option(None, "--from"),
    to_time: Optional[str] = typer.Option(None, "--to"),
    date_str: Optional[str] = typer.Option(None, "--date"),
    pricing: bool = typer.Option(
        False, "--pricing/--no-pricing",
        help="Fetch prices (requires login session)",
    ),
    output: Optional[str] = typer.Option(None, "--output", "-o"),
) -> None:
    """
    Show court availability for a SportLogic venue.

    Examples:
        python extract_sportlogic.py search picklepark
        python extract_sportlogic.py search picklepark --days 7 --pricing
        python extract_sportlogic.py search pickleplay --from 18:00 --to 22:00 --pricing
        python extract_sportlogic.py search picklepark --date 2026-05-20
    """
    console = Console()

    if venue not in VENUES:
        console.print(f"[red]Unknown venue '{venue}'. Known: {', '.join(VENUES)}[/red]")
        raise typer.Exit(1)

    if date_str:
        try:
            start_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            console.print("[red]Invalid date format. Use YYYY-MM-DD.[/red]")
            raise typer.Exit(1)
    else:
        start_date = date.today()

    from_hour = int(from_time.split(":")[0]) if from_time else 0
    to_hour = int(to_time.split(":")[0]) if to_time else 24
    dates = [start_date + timedelta(days=i) for i in range(days)]

    has_session = bool(_load_session(venue))
    if pricing and not has_session:
        console.print(f"  No cached session — attempting auto-login for {VENUES[venue]['name']}…")

    async def _run():
        all_slots = []
        # Auto-ensure valid session if pricing requested.
        jsid = None
        if pricing:
            jsid = await _ensure_valid_session(venue)
            if not jsid:
                console.print(
                    "[yellow]⚠ Could not obtain session for pricing. "
                    "Set SL_EMAIL and SL_PASSWORD in .env.[/yellow]\n"
                )

        async with SportLogicClient(venue, jsessionid=jsid) as client:
            for d in dates:
                try:
                    slots = await client.get_availability(d)
                    if pricing and jsid:
                        console.print(f"  Fetching prices for {d}…")
                        slots = await client.enrich_with_prices(slots, auto_refresh=False)
                    all_slots.extend(slots)
                except Exception as exc:
                    console.print(f"[yellow]  {d}: {exc}[/yellow]")
        return all_slots

    console.print(f"\n[bold]Scanning {VENUES[venue]['name']}…[/bold] ({days} day(s) from {start_date})\n")
    all_slots = asyncio.run(_run())

    # Filter by time.
    all_slots = [s for s in all_slots if from_hour <= s["hour"] < to_hour]

    if not all_slots:
        console.print("[yellow]No available slots found.[/yellow]")
        return

    # Save JSON.
    if output:
        out_data = {
            "venue": VENUES[venue]["name"],
            "venue_key": venue,
            "scraped_at": datetime.utcnow().isoformat() + "Z",
            "pricing_included": pricing and has_session,
            "slots": all_slots,
        }
        Path(output).write_text(json.dumps(out_data, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"[dim]Saved {len(all_slots)} slots to {output}[/dim]")

    # Display grid per date.
    all_courts = sorted(set(s["court_id"] for s in all_slots), key=lambda c: (len(c), c))
    court_names = {s["court_id"]: s["court_name"] for s in all_slots}
    by_date: dict[str, list[dict]] = {}
    for s in all_slots:
        by_date.setdefault(s["date"], []).append(s)

    for d in sorted(by_date.keys()):
        day_slots = by_date[d]
        day_name = datetime.strptime(d, "%Y-%m-%d").strftime("%A")

        table = Table(
            title=f"{VENUES[venue]['name']} · {d} ({day_name})",
            show_lines=False, padding=(0, 1),
        )
        table.add_column("Time", style="bold", width=7)
        for cid in all_courts:
            table.add_column(court_names.get(cid, cid), justify="center", width=14)

        available_map: dict[tuple, dict] = {
            (s["court_id"], s["time"]): s for s in day_slots
        }
        times = sorted(set(s["time"] for s in day_slots))

        for t in times:
            row = [t]
            for cid in all_courts:
                slot = available_map.get((cid, t))
                if slot:
                    if pricing and slot.get("price"):
                        if slot.get("is_member_price") and slot.get("full_price"):
                            row.append(
                                f"[green]{slot['price']}[/green] [dim]mbr[/dim]\n"
                                f"[yellow]{slot['full_price']}[/yellow] [dim]full[/dim]"
                            )
                        else:
                            row.append(f"[green]{slot['price']}[/green]")
                    else:
                        row.append("[green]✓[/green]")
                else:
                    row.append("[dim]—[/dim]")
            table.add_row(*row)

        console.print(table)

    total = len(all_slots)
    priced = sum(1 for s in all_slots if s.get("price"))
    member_priced = sum(1 for s in all_slots if s.get("is_member_price"))
    msg = f"\n[dim]{total} available slot(s)"
    if pricing and priced > 0:
        msg += f" · {priced} priced"
        if member_priced > 0:
            msg += f" · [green]member pricing applied[/green] (mbr = discounted, full = non-member)"
    console.print(msg + "[/dim]\n")


@cli.command(name="find")
def find_cmd(
    venues_str: str = typer.Argument(..., help="Comma-separated venue keys"),
    date_str: str = typer.Option(..., "--date"),
    from_time: str = typer.Option("06:00", "--from"),
    to_time: str = typer.Option("23:00", "--to"),
    pricing: bool = typer.Option(False, "--pricing/--no-pricing"),
) -> None:
    """
    Find available courts across multiple SportLogic venues.

    Examples:
        python extract_sportlogic.py find picklepark,pickleplay --date 2026-05-20
        python extract_sportlogic.py find picklepark,pickleplay --date 2026-05-20 --from 18:00 --to 22:00 --pricing
    """
    console = Console()

    venue_keys = [v.strip() for v in venues_str.split(",")]
    for vk in venue_keys:
        if vk not in VENUES:
            console.print(f"[red]Unknown venue '{vk}'.[/red]")
            raise typer.Exit(1)

    try:
        target_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        console.print("[red]Invalid date. Use YYYY-MM-DD.[/red]")
        raise typer.Exit(1)

    from_hour = int(from_time.split(":")[0])
    to_hour = int(to_time.split(":")[0])

    async def _find():
        all_slots = []
        for vk in venue_keys:
            try:
                # Auto-ensure session if pricing requested.
                jsid = None
                if pricing:
                    jsid = await _ensure_valid_session(vk)

                async with SportLogicClient(vk, jsessionid=jsid) as client:
                    slots = await client.get_availability(target_date)
                    slots = [s for s in slots if from_hour <= s["hour"] < to_hour]
                    if pricing and jsid:
                        console.print(f"  Fetching prices for {VENUES[vk]['name']}…")
                        slots = await client.enrich_with_prices(slots, auto_refresh=False)
                    all_slots.extend(slots)
                    console.print(f"  [green]✓[/green] {VENUES[vk]['name']}: {len(slots)} slots")
            except Exception as exc:
                console.print(f"  [red]✗[/red] {VENUES[vk]['name']}: {exc}")
        return all_slots

    day_name = target_date.strftime("%A")
    console.print(f"\n[bold]Finding courts · {target_date} ({day_name}) · {from_time}–{to_time}[/bold]\n")
    all_slots = asyncio.run(_find())

    if not all_slots:
        console.print("\n[yellow]No available slots found.[/yellow]")
        return

    table = Table(
        title=f"Available Courts · {target_date} ({day_name}) · {from_time}–{to_time}",
        show_lines=False,
    )
    table.add_column("Venue", style="bold")
    table.add_column("Time")
    table.add_column("Court")
    if pricing:
        table.add_column("Price", justify="right")

    for s in sorted(all_slots, key=lambda x: (x["venue_name"], x["time"], x["court_id"])):
        row = [s["venue_name"], s["time"], s["court_name"]]
        if pricing:
            row.append(s.get("price") or "—")
        table.add_row(*row)

    console.print(table)
    console.print(f"\n[dim]{len(all_slots)} slot(s) across {len(venue_keys)} venue(s)[/dim]\n")



if __name__ == "__main__":
    cli()