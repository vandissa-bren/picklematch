#!/usr/bin/env python3
"""
extract_thejar.py
=================

Extracts court availability, programs (Open Play / Clinics / Leagues), and the
booking calendar for **The Jar | South Melbourne** (NPL Pickleball) from the
PlayByPoint platform.

Usage
-----
    # Default: today + next 7 days, headless, write JSON + print table
    python extract_thejar.py

    # Custom date range, visible browser, logged in via .env credentials
    python extract_thejar.py --date 2026-05-20 --days 14 --no-headless

    # Different PlayByPoint club (the script is club-agnostic)
    python extract_thejar.py --slug somotherclub --base https://somotherclub.playbypoint.com

    # Only programs (skip court reservation grid)
    python extract_thejar.py --skip-courts

    # Only court grid (skip program list)
    python extract_thejar.py --skip-programs

Setup
-----
1.  Python 3.10+ recommended.
2.  Install dependencies:

        pip install -r requirements.txt
        playwright install chromium

3.  (Optional) Create a `.env` file in the same directory for authenticated
    access (some program listings are public, but booking detail / member
    pricing may require login):

        PBP_EMAIL=you@example.com
        PBP_PASSWORD=your-password

Requirements (`requirements.txt`)
---------------------------------
    playwright>=1.45
    python-dotenv>=1.0
    loguru>=0.7
    rich>=13.7
    tenacity>=8.2
    typer>=0.12
    httpx>=0.27           # used by the requests/BS4 fallback notes
    beautifulsoup4>=4.12  # used by the requests/BS4 fallback notes

Anti-bot / scraping notes
-------------------------
* `nplpickleball.playbypoint.com` and `book.nplpickleball.com.au` sit behind
  Cloudflare. A plain `requests.get` returns HTTP 403 — confirmed during
  development. That's the main reason this script defaults to Playwright:
  a real browser executes the Cloudflare JS challenge transparently.
* If you ever want to try a pure-HTTP approach (faster, no browser), see the
  `# FALLBACK (requests + BS4)` comments scattered through the file. You'll
  likely need to add `curl_cffi` or `httpx` with browser-impersonation TLS
  fingerprints, and to scrape the embedded JSON blobs PlayByPoint dumps into
  Next.js `__NEXT_DATA__` / Rails data-attributes on initial render.
* Be polite: this script adds random delays between page loads and runs
  serially. Don't crank concurrency without permission from NPL Pickleball.
* If Cloudflare starts challenging the Playwright session, options are:
    1. Run with `--no-headless` once to solve any interactive challenge;
       the persistent profile (see `USER_DATA_DIR`) keeps the clearance cookie.
    2. Add `playwright-stealth` (`pip install playwright-stealth`) and call
       `await stealth_async(page)` before navigation.
    3. Route through a residential proxy via the `--proxy` flag.

Design
------
* `PlayByPointScraper` is the only stateful class. Construct it with a config,
  call `scrape()`, get a `ScrapeResult` back. Everything else is helpers.
* Data extraction has TWO paths and tries them in order:
    1. **Network sniffing**: Playwright listens for XHR/fetch responses while
       the page loads. PlayByPoint's calendar widgets hit internal JSON
       endpoints (e.g. `/reservations.json`, `/programs.json`,
       `/api/v1/...`). Capturing those is the cleanest, most stable data
       source — no HTML parsing required.
    2. **DOM scraping**: If no useful JSON is seen, we fall back to parsing
       the rendered calendar grid and program cards. The DOM selectors are
       defined as constants near the top of `PlayByPointScraper` so they're
       easy to update when PlayByPoint ships UI changes.
* Output is normalised into the `SessionRecord` dataclass regardless of source.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import typer
from dotenv import load_dotenv
from loguru import logger
from rich.console import Console
from rich.table import Table
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# Playwright is imported lazily inside the scraper so `--help` works even
# before `playwright install` has been run.

# Optional: playwright-stealth masks obvious bot fingerprints (webdriver flag,
# missing chrome runtime, weird plugin lists). If installed, we use it.
# `pip install playwright-stealth` to enable.
#
# Library has two incompatible APIs across versions:
#   - 1.x: `from playwright_stealth import stealth_async`
#          → `await stealth_async(page)`
#   - 2.x: `from playwright_stealth import Stealth`
#          → `await Stealth().apply_stealth_async(page)`
# We try both so the script works regardless of which is installed.
_HAS_STEALTH = False
_stealth_apply = None

try:
    # 2.x API (current)
    from playwright_stealth import Stealth  # type: ignore
    _stealth_instance = Stealth()

    async def _stealth_apply(page):  # type: ignore[no-redef]
        # 2.x exposes apply_stealth_async on the Stealth instance.
        if hasattr(_stealth_instance, "apply_stealth_async"):
            await _stealth_instance.apply_stealth_async(page)
        # Some 2.x builds use a context-manager pattern only — in that
        # case there's nothing per-page to apply, so this is a no-op
        # and stealth is implied at context-creation time. Either way,
        # importing succeeded so we treat stealth as active.

    _HAS_STEALTH = True
except ImportError:
    try:
        # 1.x API (legacy)
        from playwright_stealth import stealth_async  # type: ignore

        async def _stealth_apply(page):  # type: ignore[no-redef]
            await stealth_async(page)

        _HAS_STEALTH = True
    except ImportError:
        pass

# ---------------------------------------------------------------------------
# Configuration & constants
# ---------------------------------------------------------------------------

APP_NAME = "extract_thejar"

# The authenticated hub. ALL real data lives here — the white-label
# club domains (e.g. nplpickleball.playbypoint.com) are just marketing
# surfaces and don't share sessions with `app.playbypoint.com`. Confirmed
# during development.
APP_BASE_URL = "https://app.playbypoint.com"

# Default club identity. `club_slug` is the URL slug used in
# `/book/<slug>`, `facility_id` is the numeric ID used in
# `/programs?facility_id=<id>`. Both override-able via CLI.
DEFAULT_CLUB_SLUG = "nplpickleball"
DEFAULT_FACILITY_ID = 597
DEFAULT_CLUB_NAME = "The Jar | South Melbourne"

# Legacy white-label URLs — kept only for the optional booking-page
# probe in --no-auth fallback mode.
LEGACY_CLUB_BASE_URL = "https://nplpickleball.playbypoint.com"
LEGACY_BOOKING_URL = "https://book.nplpickleball.com.au"

DEFAULT_TIMEZONE = "Australia/Melbourne"

# Where we keep Chromium profile data between runs. Persisting cookies means
# Cloudflare clearance and PBP session both survive across invocations, which
# dramatically reduces challenge hits.
USER_DATA_DIR = Path.home() / ".cache" / APP_NAME / "chromium-profile"

# How long (ms) we wait after each navigation for background XHRs to
# settle. PlayByPoint's calendar widgets lazy-load on scroll / day-change.
# 2s is usually enough; bump to 4000 if you see missing data.
NETWORK_IDLE_MS = 2000

# Pattern matching internal PBP JSON endpoints we care about. These are the
# URLs we've seen serve reservation / program data — extend as you discover
# more. Anything matching is captured into `ScrapeResult.raw_responses`.
INTERESTING_URL_RE = re.compile(
    r"(reservations|programs|sessions|schedule|availabilities|"
    r"available[_-]?hours|calendar|courts|court[_-]?types|facilities|"
    r"open[_-]?play|clinics|leagues|bookings|slots)"
    r"(\.json|/json|\?|$|/)",
    re.IGNORECASE,
)

# CSS selectors used by the DOM-scraping fallback. Defined as constants so
# they're easy to find and update when PBP ships a UI change.
SEL_PROGRAM_CARD = "[data-testid='program-card'], .program-card, a[href*='/programs/']"
SEL_RESERVATION_BLOCK = ".reservation, [data-testid='reservation-block']"
SEL_LOGIN_BUTTON = "a[href*='/sign_in'], button:has-text('Log In'), button:has-text('Login')"
SEL_EMAIL_INPUT = "input[type='email'], input[name='user[email]']"
SEL_PASSWORD_INPUT = "input[type='password'], input[name='user[password]']"
SEL_SUBMIT = "button[type='submit'], input[type='submit']"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class BookingRules:
    """
    Booking constraints for a facility. Populated from the
    /api/facilities/<id>/available_hours response's `meta.specific_rules`.
    """
    slot_length_minutes: Optional[int] = None
    min_slots_to_book: Optional[int] = None
    max_consecutive_hours: Optional[float] = None
    min_advance_hours: Optional[float] = None
    max_advance_hours: Optional[float] = None
    next_open_at: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @property
    def min_booking_minutes(self) -> Optional[int]:
        """
        Convenience: minimum booking duration in minutes.

        PBP's `minimum_time_slots_allowed_to_book` field is misleadingly
        named — its unit is actually HOURS, not slots. Confirmed by the
        on-page text "Minimum playing time is: 1 hour" when the value is
        1.0. We parse that into self.raw['_min_hours_parsed'] at capture
        time and convert to minutes here.
        """
        min_hours = self.raw.get("_min_hours_parsed")
        if isinstance(min_hours, (int, float)) and min_hours > 0:
            return int(min_hours * 60)
        # Fallback: if we somehow only got slot info, multiply.
        if (self.slot_length_minutes is not None
                and self.min_slots_to_book is not None):
            return int(self.slot_length_minutes * self.min_slots_to_book)
        return None


@dataclass
class SessionRecord:
    """
    Normalised representation of one bookable item — could be a court block,
    an open-play session, a clinic, a league night, anything.

    Fields are intentionally optional because different session types expose
    different metadata (an Open Play session has skill_level + spots, a court
    block has court_number + duration).
    """

    date: str                                  # ISO YYYY-MM-DD
    start_time: Optional[str] = None           # HH:MM (24h, club-local)
    end_time: Optional[str] = None             # HH:MM
    court_number: Optional[str] = None
    court_name: Optional[str] = None
    session_type: str = "Unknown"              # see SESSION_TYPES
    title: Optional[str] = None
    skill_level: Optional[str] = None
    spots_available: Optional[int] = None
    max_spots: Optional[int] = None
    price: Optional[str] = None                # raw string, currency-stamped
    booking_link: Optional[str] = None
    external_id: Optional[str] = None
    status: Optional[str] = None               # Available | Booked | Full | ...
    pricing_tier: Optional[str] = None         # day | primetime | peak | off-peak
    source: str = "dom"                        # "xhr" | "dom" | "manual"
    raw: dict = field(default_factory=dict)    # untouched source payload


@dataclass
class ScrapeResult:
    club_name: str
    club_slug: str
    base_url: str
    scrape_started_at: str
    scrape_finished_at: str
    start_date: str
    end_date: str
    sessions: list[SessionRecord] = field(default_factory=list)
    raw_responses: list[dict] = field(default_factory=list)  # XHR payloads
    notes: list[str] = field(default_factory=list)
    booking_rules: Optional[BookingRules] = None


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class PlayByPointScraper:
    """
    Playwright-based scraper for any PlayByPoint club.

    The class is designed to be reused for other clubs by passing a different
    `base_url` / `club_slug` — the only club-specific assumptions are the
    URL shape `{base_url}/programs` and `{base_url}/book/{slug}`, which is
    standard across PlayByPoint deployments.
    """

    def __init__(
        self,
        *,
        app_base_url: str = APP_BASE_URL,
        club_slug: str = DEFAULT_CLUB_SLUG,
        facility_id: int = DEFAULT_FACILITY_ID,
        club_name: str = DEFAULT_CLUB_NAME,
        start_date: date,
        number_of_days: int = 7,
        headless: bool = True,
        proxy: Optional[str] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
        skip_courts: bool = False,
        skip_programs: bool = False,
        dump_xhr: bool = False,
    ) -> None:
        self.app_base_url = app_base_url.rstrip("/")
        # Keep these for backward-compatibility with the old code paths
        # that referenced base_url; treat them as the app URL too.
        self.base_url = self.app_base_url
        self.booking_url = self.app_base_url
        self.club_slug = club_slug
        self.facility_id = facility_id
        self.club_name = club_name
        self.start_date = start_date
        self.number_of_days = number_of_days
        self.headless = headless
        self.proxy = proxy
        self.email = email
        self.password = password
        self.skip_courts = skip_courts
        self.skip_programs = skip_programs
        self.dump_xhr = dump_xhr

        self._raw_responses: list[dict] = []
        self._notes: list[str] = []
        self.booking_rules: Optional[BookingRules] = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def scrape(self) -> ScrapeResult:
        # Lazy import so `--help` works without Playwright installed.
        from playwright.async_api import async_playwright

        end_date = self.start_date + timedelta(days=self.number_of_days - 1)
        started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        sessions: list[SessionRecord] = []

        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as pw:
            logger.info("Launching Chromium (headless={}, proxy={})",
                        self.headless, bool(self.proxy))

            if not _HAS_STEALTH:
                logger.warning(
                    "playwright-stealth is NOT installed. Cloudflare's "
                    "non-interactive challenge fingerprints headless/"
                    "Playwright browsers and will fail this script. "
                    "Run:  pip install playwright-stealth"
                )

            launch_kwargs: dict[str, Any] = {
                "headless": self.headless,
                # Try real installed Chrome first — Cloudflare fingerprints
                # the bundled Chromium build more aggressively than stock
                # Chrome. `channel="chrome"` requires Chrome to be installed
                # on the system; we fall back to Chromium below if missing.
                "channel": "chrome",
                # Defeat the most obvious automation tells. stealth handles
                # the rest if it's available.
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-sandbox",
                ],
                "ignore_default_args": ["--enable-automation"],
            }
            if self.proxy:
                launch_kwargs["proxy"] = {"server": self.proxy}

            try:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(USER_DATA_DIR),
                    viewport={"width": 1366, "height": 900},
                    locale="en-AU",
                    timezone_id=DEFAULT_TIMEZONE,
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    **launch_kwargs,
                )
            except Exception as exc:
                # Real Chrome not installed — fall back to bundled Chromium.
                logger.warning(
                    "Could not launch real Chrome ({}). Falling back to "
                    "bundled Chromium. For best Cloudflare-bypass results, "
                    "install Chrome from https://www.google.com/chrome",
                    exc,
                )
                launch_kwargs.pop("channel", None)
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(USER_DATA_DIR),
                    viewport={"width": 1366, "height": 900},
                    locale="en-AU",
                    timezone_id=DEFAULT_TIMEZONE,
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    **launch_kwargs,
                )

            page = await context.new_page()

            # Apply stealth if available — masks bot fingerprints so
            # Cloudflare is less likely to throw Turnstile at us.
            if _HAS_STEALTH and _stealth_apply is not None:
                try:
                    await _stealth_apply(page)
                    logger.info("Stealth mode enabled.")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Stealth init failed: {} "
                                   "(continuing without stealth)", exc)
            else:
                logger.debug("playwright-stealth not installed — "
                             "Cloudflare may challenge more aggressively.")

            # Network sniffer: catch every JSON response that looks like it
            # came from PBP's calendar/program endpoints. This is the
            # primary data source.
            async def _on_response(response):
                try:
                    url = response.url
                    matches_filter = bool(INTERESTING_URL_RE.search(url))
                    # When --dump-xhr is on, we capture EVERY JSON response
                    # regardless of URL pattern. Otherwise filter to known
                    # data endpoints to keep the in-memory list small.
                    if not matches_filter and not self.dump_xhr:
                        return
                    ctype = (response.headers.get("content-type") or "").lower()
                    if "json" not in ctype:
                        return
                    try:
                        payload = await response.json()
                    except Exception:
                        # Some responses are JSON but with weird content-types
                        # or are encoded oddly — skip silently.
                        return
                    self._raw_responses.append({
                        "url": url,
                        "status": response.status,
                        "payload": payload,
                        "matched_filter": matches_filter,
                    })
                    logger.debug("Captured XHR{}: {} ({} bytes)",
                                 "" if matches_filter else " [unfiltered]",
                                 url, len(json.dumps(payload)))
                    if self.dump_xhr:
                        Path("debug").mkdir(exist_ok=True)
                        # Build a safe filename. Include enough of the
                        # URL path to distinguish endpoints — e.g.
                        # `clinic_29822_sessions` not just `sessions`.
                        # Strip the scheme + host, keep the path tail.
                        path_part = url.split("//", 1)[-1]
                        # Drop the host
                        path_part = path_part.split("/", 1)[-1] if "/" in path_part else path_part
                        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_",
                                      path_part)[:120]
                        ts = datetime.now().strftime("%H%M%S")
                        out = Path("debug") / f"xhr_{ts}_{safe}.json"
                        try:
                            out.write_text(
                                json.dumps(payload, indent=2, default=str),
                                encoding="utf-8",
                            )
                            logger.debug("  → dumped to {}", out)
                        except Exception:
                            pass
                except Exception as exc:   # noqa: BLE001
                    logger.debug("Response handler error: {}", exc)

            page.on("response", _on_response)

            try:
                # 1. Login is REQUIRED — confirmed by the booking page
                # showing a "LOGIN TO CONTINUE" gate before any slot data
                # renders. Without credentials, scraping yields nothing.
                if self.email and self.password:
                    await self._login(page)
                    if not await self._is_logged_in(page):
                        self._notes.append(
                            "Login appears to have failed — check credentials."
                        )
                        logger.error("Not logged in after login attempt — "
                                     "bailing. Set PBP_EMAIL / PBP_PASSWORD "
                                     "correctly in .env and retry.")
                        await self._dump_debug(page, "login_failed")
                        return ScrapeResult(
                            club_name=self.club_name, club_slug=self.club_slug,
                            base_url=self.app_base_url,
                            scrape_started_at=started_at,
                            scrape_finished_at=datetime.now(
                                timezone.utc).isoformat().replace("+00:00", "Z"),
                            start_date=self.start_date.isoformat(),
                            end_date=end_date.isoformat(),
                            sessions=[], raw_responses=self._raw_responses,
                            notes=self._notes,
                        )
                else:
                    msg = ("PlayByPoint requires login before showing slot "
                           "data. Set PBP_EMAIL and PBP_PASSWORD in a .env "
                           "file and retry — otherwise this run will return 0 "
                           "sessions.")
                    logger.warning(msg)
                    self._notes.append(msg)

                # 2. Programs (Open Play, Clinics, Leagues).
                if not self.skip_programs:
                    program_sessions = await self._scrape_programs(page)
                    sessions.extend(program_sessions)
                else:
                    logger.info("Skipping programs (--skip-programs)")

                # 3. Court booking grid: a single SPA navigation that
                #    clicks date buttons internally.
                if not self.skip_courts:
                    court_sessions = await self._scrape_court_grid_all_days(page)
                    sessions.extend(court_sessions)
                else:
                    logger.info("Skipping court grid (--skip-courts)")

            finally:
                await context.close()

        finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        return ScrapeResult(
            club_name=self.club_name,
            club_slug=self.club_slug,
            base_url=self.app_base_url,
            scrape_started_at=started_at,
            scrape_finished_at=finished_at,
            start_date=self.start_date.isoformat(),
            end_date=end_date.isoformat(),
            sessions=sessions,
            raw_responses=self._raw_responses,
            notes=self._notes,
            booking_rules=self.booking_rules,
        )

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _login(self, page) -> None:
        """
        Sign in via PlayByPoint's standard Devise-based login form.

        We don't fail hard on login errors — if login fails we still scrape
        public data and add a note to the result.

        PBP's login lives at https://app.playbypoint.com/users/sign_in
        (canonical across all clubs — confirmed in PBP's help docs). The
        session cookie is set on `.playbypoint.com`, so after authenticating
        we navigate to the club's subdomain and the session carries over.

        The form may also include an account-type radio ("Player Account /
        Organization Access / Staff & Admin Access") — we default to Player.
        On some deployments it's a single combined email+password form;
        on others it's a two-step "enter email → continue → enter password"
        flow. We handle both.
        """
        login_url = "https://app.playbypoint.com/users/sign_in"
        logger.info("Logging in as {} via {} …", self.email, login_url)
        try:
            await self._goto_with_retry(page, login_url)

            # Cloudflare's non-interactive challenge can take 5-20 seconds
            # to run silently before redirecting to the real page. Give it
            # plenty of time before we even start looking for form inputs.
            logger.info("Waiting for Cloudflare to settle …")
            settled = False
            for attempt in range(20):  # up to ~40s
                await page.wait_for_timeout(2000)
                title = (await page.title()).strip().lower()
                if title not in ("just a moment...", "just a moment…",
                                 "attention required! | cloudflare", ""):
                    logger.info("Page title is now: {!r}", title)
                    settled = True
                    break
                logger.debug("Still on CF interstitial ({}s) — title={!r}",
                             (attempt + 1) * 2, title)
            if not settled:
                logger.error("Cloudflare did not pass through after 40s. "
                             "See debug/login_cf_stuck.* for what was rendered.")
                await self._dump_debug(page, "login_cf_stuck")

            # If Cloudflare interstitial appeared, wait for human / give up.
            # We tell it what "real login page" looks like so it doesn't
            # accept a transient challenge-UI flicker as success.
            if not await self._wait_for_cloudflare(
                page,
                expected_selectors=[
                    "input[type='email']",
                    "input[name='user[email]']",
                    "input[type='password']",
                    "form[action*='sign_in']",
                ],
            ):
                self._notes.append(
                    "Cloudflare challenge not cleared — login aborted."
                )
                return

            # Step 1: select 'Player Account' radio if present. PBP styles
            # these as custom cards — the actual <input> is often
            # display:none with the visible UI being a <label>. Use
            # force=True since we know the click target is intentionally
            # styled-but-functional. Wrapped in tight try/except because
            # this whole step is best-effort.
            try:
                for sel in (
                    "label:has-text('Player Account')",
                    "label:has-text('Book Courts')",
                    "[role='radio']:has-text('Player')",
                    "input[value='player']",
                ):
                    el = await page.query_selector(sel)
                    if el:
                        try:
                            await el.click(force=True, timeout=3000)
                            await page.wait_for_timeout(500)
                            break
                        except Exception:
                            continue
            except Exception:
                pass

            # Step 2: fill email. Wait for the input to appear (up to 10s)
            # rather than relying on the default 30s page-fill timeout.
            # PBP wraps Devise in a custom 'SSO' UI with stable IDs —
            # those are the most reliable selectors. The generic fallbacks
            # come after.
            email_filled = await self._try_fill(
                page,
                [
                    "#sso-email-input",
                    "input[type='email']",
                    "input[name='user[email]']",
                    "input[name='email']",
                    "input[autocomplete='email']",
                    "input[placeholder*='mail' i]",
                ],
                self.email or "",
                timeout_ms=10000,
            )
            if not email_filled:
                logger.error("Could not find email input on login page.")
                await self._dump_debug(page, "login_no_email_input")
                self._notes.append("Login failed: email input not found. "
                                   "See debug/login_no_email_input.*")
                return

            # Step 3: fill password. If the form is two-step we may need to
            # click 'Continue' / 'Next' first to reveal the password field.
            password_filled = await self._try_fill(
                page,
                [
                    "#sso-password-input",
                    "input[type='password']",
                    "input[name='user[password]']",
                    "input[name='password']",
                    "input[autocomplete='current-password']",
                ],
                self.password or "",
                timeout_ms=2000,
            )
            if not password_filled:
                logger.debug("Password input not visible — looking for a "
                             "'Continue' button (two-step form).")
                for sel in ("button:has-text('Continue')",
                            "button:has-text('Next')",
                            "button[type='submit']"):
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        break
                await page.wait_for_timeout(1000)
                password_filled = await self._try_fill(
                    page,
                    [
                        "input[type='password']",
                        "input[name='user[password]']",
                    ],
                    self.password or "",
                    timeout_ms=10000,
                )
            if not password_filled:
                logger.error("Could not find password input on login page.")
                await self._dump_debug(page, "login_no_password_input")
                self._notes.append("Login failed: password input not found. "
                                   "See debug/login_no_password_input.*")
                return

            # Verify the inputs actually hold our values before we click
            # submit — React-controlled inputs can swallow keystrokes
            # silently if focus is lost or component re-renders mid-type.
            await self._verify_login_inputs(page, label="Main")

            # Step 4: submit. PBP's `#sso-sign-in-btn` is the real button
            # — the visible green LOGIN button. There's also a hidden
            # `<input type="submit">` (Devise fallback) that LOOKS clickable
            # but skips PBP's JS, causing silent submission failures. So
            # we target the SSO button explicitly. `_click_any` escalates
            # through native → force → JS-evaluate to defeat actionability
            # quirks in PBP's animated SSO widget.
            clicked = await self._click_any(
                page,
                [
                    "#sso-sign-in-btn",
                    "button[type='submit']:has-text('LOGIN')",
                    "button:has-text('LOGIN')",
                    "button[type='submit']:has-text('Sign In')",
                    "button:has-text('Sign In')",
                    "button:has-text('Log In')",
                    "button:has-text('Login')",
                ],
                timeout_ms=8000,
                label="Main login submit",
            )
            if not clicked:
                logger.debug("No SSO button worked — pressing Enter on the "
                             "password field as fallback.")
                try:
                    pw_input = await page.query_selector(
                        "#sso-password-input, input[type='password']"
                    )
                    if pw_input:
                        await pw_input.press("Enter")
                    else:
                        await page.keyboard.press("Enter")
                except Exception:
                    await page.keyboard.press("Enter")

            # Wait for redirect away from sign_in.
            try:
                await page.wait_for_url(
                    lambda u: "sign_in" not in u, timeout=15000
                )
            except Exception:
                pass

            if "sign_in" in page.url:
                # Look for visible error text PBP shows on bad credentials.
                err = await self._safe_text(
                    page,
                    ".flash-error, .alert-danger, [class*='error']:visible",
                )
                self._notes.append(
                    f"Login failed (still on sign_in). Error: {err or 'none shown'}"
                )
                logger.warning("Login failed (still on sign_in). "
                               "Page error text: {}", err)
                await self._dump_debug(page, "login_failed")
                return

            logger.success("Main login OK. Landed on: {}", page.url)

            # NOTE: We do NOT need a second login on the white-label club
            # domain (e.g. nplpickleball.playbypoint.com). All scraping
            # happens on the authenticated `app.playbypoint.com` hub, which
            # has visibility into every facility you're a member of via
            # `/book/<slug>` and `/programs?facility_id=<id>`.
        except Exception as exc:  # noqa: BLE001
            self._notes.append(f"Login error: {exc}")
            logger.warning("Login error (continuing anonymously): {}", exc)
            await self._dump_debug(page, "login_exception")

    async def _login_club(self, page) -> None:
        """
        Second login pass on the club subdomain — same form as the parent
        site but a separate session. Without this, booking-grid and program
        detail XHRs come back empty / 401.
        """
        club_login_url = f"{self.base_url}/users/sign_in"
        logger.info("Logging in to club site: {}", club_login_url)
        try:
            await self._goto_with_retry(page, club_login_url)
            await page.wait_for_timeout(1500)

            # If the club site already considers us logged in, the
            # /users/sign_in URL redirects elsewhere — bail early.
            if "sign_in" not in page.url:
                logger.success("Club site already logged in via parent "
                               "session (redirected to {}).", page.url)
                return

            # Same Cloudflare dance, same form layout as the parent.
            if not await self._wait_for_cloudflare(
                page,
                expected_selectors=[
                    "input[type='email']",
                    "input[name='user[email]']",
                    "input[type='password']",
                ],
            ):
                self._notes.append(
                    "Cloudflare not cleared on club site — booking-grid "
                    "data will be unavailable."
                )
                return

            # Account-type radio (best-effort, same as parent).
            try:
                for sel in (
                    "label:has-text('Player Account')",
                    "label:has-text('Book Courts')",
                    "[role='radio']:has-text('Player')",
                    "input[value='player']",
                ):
                    el = await page.query_selector(sel)
                    if el:
                        try:
                            await el.click(force=True, timeout=3000)
                            await page.wait_for_timeout(500)
                            break
                        except Exception:
                            continue
            except Exception:
                pass

            email_filled = await self._try_fill(
                page,
                [
                    "#sso-email-input",
                    "input[type='email']",
                    "input[name='user[email]']",
                    "input[name='email']",
                    "input[autocomplete='email']",
                ],
                self.email or "",
                timeout_ms=10000,
            )
            if not email_filled:
                logger.warning("Club login: email field not found.")
                await self._dump_debug(page, "club_login_no_email")
                return

            password_filled = await self._try_fill(
                page,
                [
                    "#sso-password-input",
                    "input[type='password']",
                    "input[name='user[password]']",
                    "input[name='password']",
                    "input[autocomplete='current-password']",
                ],
                self.password or "",
                timeout_ms=5000,
            )
            if not password_filled:
                # Two-step form path.
                for sel in ("button:has-text('Continue')",
                            "button:has-text('Next')",
                            "button[type='submit']"):
                    btn = await page.query_selector(sel)
                    if btn:
                        try:
                            await btn.click(force=True, timeout=3000)
                        except Exception:
                            pass
                        break
                await page.wait_for_timeout(1000)
                password_filled = await self._try_fill(
                    page,
                    [
                        "input[type='password']",
                        "input[name='user[password]']",
                    ],
                    self.password or "",
                    timeout_ms=10000,
                )
            if not password_filled:
                logger.warning("Club login: password field not found.")
                await self._dump_debug(page, "club_login_no_password")
                return

            await self._verify_login_inputs(page, label="Club")

            clicked = await self._click_any(
                page,
                [
                    "#sso-sign-in-btn",
                    "button[type='submit']:has-text('LOGIN')",
                    "button:has-text('LOGIN')",
                    "button[type='submit']:has-text('Sign In')",
                    "button:has-text('Sign In')",
                    "button:has-text('Log In')",
                    "button:has-text('Login')",
                ],
                timeout_ms=8000,
                label="Club login submit",
            )
            if not clicked:
                logger.debug("No SSO button worked — pressing Enter on the "
                             "password field as fallback.")
                try:
                    pw_input = await page.query_selector(
                        "#sso-password-input, input[type='password']"
                    )
                    if pw_input:
                        await pw_input.press("Enter")
                    else:
                        await page.keyboard.press("Enter")
                except Exception:
                    await page.keyboard.press("Enter")

            try:
                await page.wait_for_url(
                    lambda u: "sign_in" not in u, timeout=15000
                )
            except Exception:
                pass

            if "sign_in" in page.url:
                err = await self._safe_text(
                    page,
                    ".flash-error, .alert-danger, [class*='error']:visible",
                )
                logger.warning("Club login failed. Page error: {}", err)
                self._notes.append(
                    f"Club login failed: {err or 'no error shown'}"
                )
                await self._dump_debug(page, "club_login_failed")
            else:
                logger.success("Club login OK. Landed on: {}", page.url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Club login error (continuing): {}", exc)
            self._notes.append(f"Club login error: {exc}")

    async def _verify_login_inputs(self, page, label: str = "") -> None:
        """
        Read the current values of the email + password fields back via
        JavaScript (which sees the live DOM `.value`, the same thing PBP's
        submit handler will read) and warn if they look empty. This is
        purely diagnostic — we don't abort if they look wrong, because
        the values might still be readable via React's internal state
        even when `.value` looks stale. But the log line is critical for
        debugging: if you see `email=0 chars, password=0 chars`, you know
        instantly why the submission silently failed.
        """
        try:
            state = await page.evaluate(
                """
                () => {
                    const e = document.querySelector('#sso-email-input')
                          || document.querySelector("input[type='email']");
                    const p = document.querySelector('#sso-password-input')
                          || document.querySelector("input[type='password']");
                    return {
                        emailLen: e ? (e.value || '').length : -1,
                        passwordLen: p ? (p.value || '').length : -1,
                    };
                }
                """
            )
            logger.debug(
                "{} login pre-submit: email={} chars, password={} chars",
                label, state.get("emailLen"), state.get("passwordLen"),
            )
            if state.get("emailLen", 0) <= 0 or state.get("passwordLen", 0) <= 0:
                logger.warning(
                    "{} login: input value(s) appear empty in DOM. "
                    "Fill may not have stuck — submission will likely fail.",
                    label,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("{} login: value verification failed: {}",
                         label, exc)

    async def _try_fill(
        self, page, selectors: list[str], value: str, timeout_ms: int = 5000
    ) -> bool:
        """
        Try a list of selectors in order, filling the first one that's
        attached within `timeout_ms`. Returns True on success.

        IMPORTANT: PBP's SSO widget uses React-controlled inputs. We've
        seen two failure modes with naive approaches:

        1. `page.fill()`        — sets DOM .value but React state stays
                                  empty → form submits with blank fields.
        2. `press_sequentially` — types real keystrokes, but clicking
                                  into the next field can blur the prior
                                  one and trigger handlers that wipe it.

        So we use a third approach: a single JS call per field that
        (a) uses the React-recommended `nativeInputValueSetter` pattern
        to bypass React's state-tracking, (b) dispatches `input` AND
        `change` events so React's `onChange` fires. This is the same
        trick automated UI tests use to fill controlled inputs. No
        focus changes, no keyboard events, no opportunity for blur
        handlers to interfere.
        """
        per_selector_timeout = max(timeout_ms // len(selectors), 500)
        for sel in selectors:
            try:
                await page.wait_for_selector(
                    sel, state="attached", timeout=per_selector_timeout
                )
                result = await page.evaluate(
                    """
                    ([selector, value]) => {
                        const el = document.querySelector(selector);
                        if (!el) return { ok: false, reason: 'not_found' };
                        // React tracks input values via an internal
                        // `_valueTracker`. Calling the native setter
                        // forces React to notice the change.
                        const proto = Object.getPrototypeOf(el);
                        const setter = Object.getOwnPropertyDescriptor(
                            proto, 'value'
                        )?.set;
                        if (setter) {
                            setter.call(el, value);
                        } else {
                            el.value = value;
                        }
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return { ok: true, length: el.value.length };
                    }
                    """,
                    [sel, value],
                )
                if result and result.get("ok"):
                    logger.debug("Filled {} ({} chars) via JS",
                                 sel, result.get("length"))
                    return True
            except Exception as exc:
                logger.debug("Fill failed on {}: {}", sel, exc)
                continue
        return False

    async def _click_any(
        self, page, selectors: list[str], timeout_ms: int = 5000,
        label: str = "button",
    ) -> Optional[str]:
        """
        Click the first matching selector that we can actually activate.

        Playwright's `.click()` enforces visibility / actionability rules
        that PBP's SSO widget (with its animations and aria-hidden ancestors)
        can fail even when the element is functionally clickable. So this
        helper tries three escalating strategies per selector:

          1. Wait for visible + native `.click()`
          2. Native `.click(force=True)`
          3. JavaScript `.click()` via page.evaluate — bypasses all
             Playwright actionability checks; effectively a direct DOM
             invocation. Equivalent to typing
             `document.querySelector(sel).click()` in DevTools.

        Returns the selector that worked, or None.
        """
        for sel in selectors:
            # Strategy 1 + 2: native click
            try:
                el = await page.wait_for_selector(
                    sel, state="attached", timeout=timeout_ms,
                )
                if el is None:
                    continue
                # Try normal click first.
                try:
                    await el.click(timeout=2000)
                    logger.debug("{} clicked via native click: {}", label, sel)
                    return sel
                except Exception:
                    pass
                # Then forced.
                try:
                    await el.click(force=True, timeout=2000)
                    logger.debug("{} clicked via force click: {}", label, sel)
                    return sel
                except Exception:
                    pass
                # Then JS click — the ultimate bypass.
                try:
                    await page.evaluate(
                        "(s) => { const el = document.querySelector(s); "
                        "if (el) el.click(); }",
                        sel,
                    )
                    logger.debug("{} clicked via JS evaluate: {}", label, sel)
                    return sel
                except Exception as exc:
                    logger.debug("{} JS click failed on {}: {}",
                                 label, sel, exc)
                    continue
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Programs
    # ------------------------------------------------------------------

    async def _scrape_programs(self, page) -> list[SessionRecord]:
        """
        Discover programs via the public catalog endpoint
            GET /api/public/clinics?search=
        which returns ALL active programs for the facility in one shot
        (~12KB), with fields: id, name, category, capacity, start_date,
        end_date, future_week_days, ntrp_str, url.

        We then:
          1. Expand each catalog entry into per-date "session" records by
             intersecting its date range + future_week_days with the
             user-requested date window.
          2. Visit each program detail page to capture session-level data
             (times, prices, current spots available) that the catalog
             endpoint doesn't include. Detail data, when found, is merged
             in by matching date.

        FALLBACK (requests + BS4):
            r = httpx.get(f"{self.base_url}/api/public/clinics?search=")
            payload = r.json()
            ... same expansion logic
            This endpoint is PUBLIC (no auth required) — confirmed during
            development — so a pure-HTTP scrape works IF you can get past
            Cloudflare (use curl_cffi with browser impersonation).
        """
        # Programs listing on the authenticated hub. The `facility_id`
        # query param scopes the results to The Jar (or whichever club).
        url = (f"{self.app_base_url}/programs"
               f"?facility_id={self.facility_id}&category=&search=&view=grid")
        logger.info("Fetching programs index: {}", url)
        await self._goto_with_retry(page, url)
        await page.wait_for_timeout(NETWORK_IDLE_MS)
        await self._wait_for_cloudflare(
            page,
            expected_selectors=["a[href*='/programs/']", "main", "[role='main']"],
        )

        # Extract the clinics catalog from our captured XHRs.
        catalog = self._find_clinics_payload()
        if not catalog:
            logger.warning("No /api/public/clinics payload captured. "
                           "Falling back to scraping each program page "
                           "individually.")
            await self._dump_debug(page, "no_clinics_payload")
            self._notes.append("Clinics catalog not captured — programs "
                               "data may be incomplete.")
            return await self._scrape_programs_via_pages(page)

        logger.info("Catalog returned {} programs.", len(catalog))

        # Expand catalog entries into per-date session stubs across the
        # user's requested window.
        sessions = self._expand_catalog(catalog)
        logger.info("Expanded to {} per-date session stubs across "
                    "{}..{}.", len(sessions),
                    self.start_date.isoformat(),
                    (self.start_date + timedelta(days=self.number_of_days - 1)
                     ).isoformat())

        # Visit each program detail page to enrich the stubs with times,
        # prices, spots-available, etc. We only visit programs that have at
        # least one occurrence in our window — saves time on inactive ones.
        active_slugs = {
            (s.booking_link.rsplit("/", 1)[-1] if s.booking_link else "")
            for s in sessions
        }
        active_slugs.discard("")
        logger.info("{} of {} programs are active in this window — "
                    "visiting their detail pages for times/prices/spots …",
                    len(active_slugs), len(catalog))

        for idx, slug in enumerate(sorted(active_slugs), start=1):
            detail_url = f"{self.app_base_url}/programs/{slug}"
            logger.info("Program {}/{}: {}", idx, len(active_slugs), slug)
            try:
                enrichments = await self._scrape_program_detail_xhrs(
                    page, detail_url
                )
                # Merge enrichments back into matching stubs by program slug
                # + date.
                matched = self._merge_enrichments(sessions, slug, enrichments)
                logger.info("  → enriched {} session(s)", matched)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Detail scrape failed for {}: {}", slug, exc)
                self._notes.append(f"Detail scrape failed: {slug} ({exc})")
            await self._polite_delay()

        return sessions

    def _find_clinics_payload(self) -> list[dict]:
        """Locate the most recent /api/public/clinics response, if any."""
        for entry in reversed(self._raw_responses):
            url = entry.get("url", "")
            if "/api/public/clinics" not in url:
                continue
            payload = entry.get("payload") or {}
            clinics = payload.get("clinics")
            if isinstance(clinics, list):
                return clinics
        return []

    # PBP's future_week_days field uses 0=Sunday, 1=Monday, ..., 6=Saturday
    # (US JS convention). Python's date.weekday() uses 0=Monday, so we map.
    _PBP_WEEKDAY_TO_PY = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}

    def _expand_catalog(self, catalog: list[dict]) -> list[SessionRecord]:
        """
        Turn each catalog entry into one SessionRecord per matching date
        in the requested window. Times/prices come later (from detail).
        """
        end_date = self.start_date + timedelta(days=self.number_of_days - 1)
        sessions: list[SessionRecord] = []

        for prog in catalog:
            try:
                prog_start = datetime.strptime(
                    prog.get("start_date", ""), "%Y-%m-%d"
                ).date()
                prog_end = datetime.strptime(
                    prog.get("end_date", ""), "%Y-%m-%d"
                ).date()
            except ValueError:
                continue

            # The program's recurring weekdays, converted to Python convention.
            pbp_days = prog.get("future_week_days") or []
            py_days = {self._PBP_WEEKDAY_TO_PY.get(d) for d in pbp_days}
            py_days.discard(None)
            if not py_days:
                continue

            # Walk each day in the user's window. Include only if the
            # program is active that day AND its weekday matches.
            window_start = max(self.start_date, prog_start)
            window_end = min(end_date, prog_end)
            if window_start > window_end:
                continue

            day = window_start
            while day <= window_end:
                if day.weekday() in py_days:
                    slug = (prog.get("url") or "").rsplit("/", 1)[-1]
                    sessions.append(SessionRecord(
                        date=day.isoformat(),
                        title=prog.get("name"),
                        session_type=_normalise_category(prog.get("category")),
                        skill_level=prog.get("ntrp_str") or None,
                        max_spots=prog.get("capacity"),
                        booking_link=(
                            f"{self.app_base_url}{prog.get('url')}"
                            if prog.get("url") else None
                        ),
                        external_id=str(prog.get("id")) if prog.get("id") else None,
                        source="xhr",
                        raw={
                            "catalog": prog,
                            "slug": slug,
                        },
                    ))
                day += timedelta(days=1)

        return sessions

    async def _scrape_program_detail_xhrs(
        self, page, url: str
    ) -> list[dict]:
        """
        Visit a program detail page and extract its sessions.

        PBP renders the detail page server-side as Rails HTML containing
        a React mount point:

            <div data-react-class="ClinicStepperIndividualSesions"
                 data-react-props="{ ... HTML-encoded JSON ... }">

        The `data-react-props` JSON contains everything we need:
        clinic_id, sessions[] (with lesson_date, hour_start/hour_end as
        seconds-since-midnight, player_count, capacity, status,
        teacher_names, formatted_hour_start), and packages[] (price tiers).

        No XHR fires — it's all in the initial HTML payload.

        Returns a single-element list shaped like our captured-XHR entries
        so the existing merge logic in _merge_enrichments can consume it.
        """
        await self._goto_with_retry(page, url)
        await page.wait_for_timeout(NETWORK_IDLE_MS)

        # One-time HTML dump of the first detail page for inspection.
        if self.dump_xhr and not getattr(self, "_dumped_detail_html", False):
            try:
                Path("debug").mkdir(exist_ok=True)
                html = await page.content()
                out = Path("debug") / "first_program_detail.html"
                out.write_text(html, encoding="utf-8")
                logger.debug("Dumped first program detail HTML to {}", out)
                self._dumped_detail_html = True
            except Exception:
                pass

        # Extract the React-component props via JS — far easier than
        # parsing HTML-encoded JSON ourselves.
        try:
            props_json = await page.evaluate(
                """
                () => {
                    const el = document.querySelector(
                        "[data-react-class='ClinicStepperIndividualSesions']"
                    );
                    if (!el) return null;
                    // The attribute is HTML-encoded JSON. The browser
                    // automatically decodes when we read .dataset, but
                    // we read .getAttribute then JSON.parse for clarity.
                    return el.getAttribute('data-react-props');
                }
                """
            )
        except Exception as exc:
            logger.debug("Could not evaluate react-props extraction: {}", exc)
            return []

        if not props_json:
            logger.debug("No ClinicStepperIndividualSesions component on "
                         "page — program may use a different layout.")
            return []

        try:
            props = json.loads(props_json)
        except Exception as exc:
            logger.debug("Failed to parse react-props JSON: {}", exc)
            return []

        # Wrap in our standard XHR-entry shape so _merge_enrichments
        # treats it like any other captured payload.
        return [{
            "url": f"{url}#data-react-props",
            "status": 200,
            "payload": props,
        }]

    def _merge_enrichments(
        self,
        sessions: list[SessionRecord],
        slug: str,
        enrichments: list[dict],
    ) -> int:
        """
        Merge program-detail data into the catalog stubs.

        PBP's detail payload (from the `ClinicStepperIndividualSesions`
        React-component props) has this shape:

            {
              "clinic_id": 91940,
              "clinic_name": "...",
              "sessions": [
                {
                  "id": 1563599,
                  "lesson_date": "2025-03-04",
                  "hour_start": 25200,    // seconds since midnight
                  "hour_end": 30600,
                  "player_count": 5,
                  "capacity": 8,
                  "status": "checked",     // 'checked' = booked-day,
                                           // 'pending' = future open day
                  "teacher_names": ["..."],
                  "short_schedule": "7-8:30am",
                  "formatted_hour_start": "07:00 AM",
                  "individual_prices": [
                    {"player_category":"member",    "price":30.0},
                    {"player_category":"non_member","price":35.0}
                  ]
                }, ...
              ],
              "packages": [...],  // bulk pricing
              "currentUserAffiliation": "non_member"
            }

        For each session in the payload, we find the catalog stub with
        the matching slug + date and merge in the per-session times,
        prices (using the user's affiliation tier when available), spots
        available, and teacher.
        """
        matched = 0
        for entry in enrichments:
            payload = entry.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            detail_sessions = payload.get("sessions") or []
            if not detail_sessions:
                continue

            # Pick a sensible price tier. PBP exposes 'member' vs
            # 'non_member' rates; we use the logged-in user's affiliation
            # if available, otherwise fall back to non_member (the public
            # rate non-members see).
            user_affiliation = (payload.get("currentUserAffiliation")
                                or payload.get("current_user_affiliation")
                                or "non_member")
            currency_prefix = "$"

            # Extract fallback price from packages or top level.
            fallback_price: Optional[float] = None
            for pkg in (payload.get("packages")
                        or payload.get("clinic_packages") or []):
                if isinstance(pkg, dict):
                    v = pkg.get("price") or pkg.get("amount")
                    if isinstance(v, (int, float)) and v > 0:
                        fallback_price = float(v)
                        break
            if fallback_price is None:
                for pk in ("price", "amount", "cost", "fee"):
                    v = payload.get(pk)
                    if isinstance(v, (int, float)) and v > 0:
                        fallback_price = float(v)
                        break

            for ds in detail_sessions:
                if not isinstance(ds, dict):
                    continue
                iso_date = ds.get("lesson_date")
                if not iso_date:
                    continue

                # Convert seconds-since-midnight → HH:MM.
                hour_start = ds.get("hour_start")
                hour_end = ds.get("hour_end")
                start_time = _seconds_to_hhmm(hour_start) if isinstance(
                    hour_start, (int, float)) else None
                end_time = _seconds_to_hhmm(hour_end) if isinstance(
                    hour_end, (int, float)) else None

                # Pick the price matching the user's affiliation; if no
                # match, take the first non-member tier (public rate).
                price_str: Optional[str] = None

                # Strategy 1: individual_prices[] with player_category.
                for p in (ds.get("individual_prices") or []):
                    if (isinstance(p, dict)
                            and p.get("player_category") == user_affiliation
                            and "price" in p):
                        price_str = f"{currency_prefix}{float(p['price']):.2f}"
                        break
                if price_str is None:
                    for p in (ds.get("individual_prices") or []):
                        if (isinstance(p, dict)
                                and p.get("player_category") == "non_member"
                                and "price" in p):
                            price_str = (
                                f"{currency_prefix}{float(p['price']):.2f}"
                            )
                            break

                # Strategy 2: direct price on session (ClinicStepper).
                if price_str is None:
                    for pk in ("price", "amount", "cost", "fee",
                               "session_price", "per_session_price"):
                        v = ds.get(pk)
                        if isinstance(v, (int, float)) and v > 0:
                            price_str = f"{currency_prefix}{float(v):.2f}"
                            break

                # Strategy 3: prices[] array on session.
                if price_str is None:
                    for p in (ds.get("prices") or []):
                        if isinstance(p, dict):
                            v = p.get("price") or p.get("amount")
                            if isinstance(v, (int, float)) and v > 0:
                                price_str = f"{currency_prefix}{float(v):.2f}"
                                break

                # Strategy 4: package/top-level fallback.
                if price_str is None and fallback_price:
                    price_str = f"{currency_prefix}{fallback_price:.2f}"

                player_count = ds.get("player_count")
                capacity = ds.get("capacity")
                spots_available = None
                if isinstance(player_count, int) and isinstance(capacity, int):
                    spots_available = max(capacity - player_count, 0)

                # Status mapping: PBP uses 'checked' (past, attendance taken),
                # 'pending' (future, bookings open). Translate to a more
                # user-friendly Available/Full/Past.
                pbp_status = ds.get("status")
                if (isinstance(spots_available, int)
                        and isinstance(capacity, int)
                        and capacity > 0):
                    if spots_available == 0:
                        status = "Full"
                    else:
                        status = "Available"
                elif pbp_status == "checked":
                    status = "Past"
                else:
                    status = pbp_status or None

                teachers = ds.get("teacher_names") or []
                teacher_str = ", ".join(t for t in teachers if t) or None

                # Find matching catalog stub.
                merged_this_session = False
                for stub in sessions:
                    if (stub.raw.get("slug") == slug
                            and stub.date == iso_date):
                        if start_time:
                            stub.start_time = start_time
                        if end_time:
                            stub.end_time = end_time
                        if price_str:
                            stub.price = price_str
                        if spots_available is not None:
                            stub.spots_available = spots_available
                        if isinstance(capacity, int):
                            stub.max_spots = capacity
                        if status:
                            stub.status = status
                        if teacher_str:
                            # Tack the teacher onto the title for the
                            # printed table — keeps the table compact
                            # while preserving the data in `raw`.
                            base_title = stub.title or ""
                            if teacher_str not in (base_title or ""):
                                stub.title = f"{base_title} · {teacher_str}".strip(" ·")
                        stub.external_id = str(ds.get("id")) or stub.external_id
                        stub.raw["detail"] = ds
                        matched += 1
                        merged_this_session = True
                        break

                # If the detail has a session we didn't have a stub for
                # (e.g. the catalog's future_week_days lied, or it's an
                # ad-hoc one-off session), add it as a new record.
                if not merged_this_session and iso_date:
                    try:
                        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    window_end = self.start_date + timedelta(
                        days=self.number_of_days - 1
                    )
                    if not (self.start_date <= d <= window_end):
                        continue
                    sessions.append(SessionRecord(
                        date=iso_date,
                        start_time=start_time,
                        end_time=end_time,
                        session_type="Program",
                        title=teacher_str or None,
                        spots_available=spots_available,
                        max_spots=capacity if isinstance(capacity, int)
                        else None,
                        price=price_str,
                        status=status,
                        booking_link=f"{self.app_base_url}/programs/{slug}",
                        external_id=str(ds.get("id")) if ds.get("id") else None,
                        source="dom",
                        raw={"slug": slug, "detail": ds, "ad_hoc": True},
                    ))
                    matched += 1
        return matched

    async def _scrape_programs_via_pages(self, page) -> list[SessionRecord]:
        """
        Last-resort fallback: if the /api/public/clinics catalog didn't
        load, fall back to the old per-page scraping path. Less reliable
        but better than returning nothing.
        """
        hrefs = await page.eval_on_selector_all(
            "a[href*='/programs/']",
            "els => Array.from(new Set(els.map(e => e.href)))",
        )
        detail_urls = [
            h for h in hrefs if re.search(r"/programs/[^/?#]+$", h)
        ]
        sessions: list[SessionRecord] = []
        for url in detail_urls:
            try:
                sessions.extend(await self._scrape_program_detail(page, url))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Per-page fallback failed for {}: {}",
                               url, exc)
            await self._polite_delay()
        return sessions

    async def _scrape_program_detail(self, page, url: str) -> list[SessionRecord]:
        """
        Parse a single program page. Most useful data tends to be in the
        captured XHRs (sessions per program), but we also pull header text
        as a safety net.
        """
        logger.debug("Program detail: {}", url)
        await self._goto_with_retry(page, url)
        await page.wait_for_timeout(NETWORK_IDLE_MS)

        # Grab the visible header for title / pricing as a fallback.
        title = await self._safe_text(page, "h1")
        # Price strings on PBP detail pages are usually rendered as e.g.
        # "$20.00" inside a pricing block.
        price = await self._safe_text(
            page,
            "[class*='price'], [data-testid*='price']",
        )

        # Try to read structured session listings from the DOM. PlayByPoint
        # renders upcoming sessions as a list/table; we tolerate either.
        rows = await page.query_selector_all(
            "[data-testid='session-row'], "
            ".session-row, "
            "li[class*='session'], "
            "tr[class*='session']"
        )

        sessions: list[SessionRecord] = []
        for row in rows:
            text = (await row.inner_text()).strip()
            # Try to find a date / time block in the row text — keeps things
            # working even when PBP renames class names.
            session = self._parse_session_row_text(
                text,
                program_url=url,
                default_title=title,
                default_price=price,
            )
            if session:
                sessions.append(session)

        # If the DOM yielded nothing but we captured XHRs that look like
        # session lists, normalise from those instead.
        if not sessions:
            sessions.extend(self._sessions_from_xhrs(default_title=title,
                                                    program_url=url,
                                                    default_price=price))
        return sessions

    # ------------------------------------------------------------------
    # Court grid
    # ------------------------------------------------------------------

    async def _scrape_court_grid_all_days(self, page) -> list[SessionRecord]:
        """
        Scrape per-slot court availability for every day in the window.

        PBP's booking page fires:
            GET /api/facilities/<id>/available_hours
                ?timestamp=<unix>
                &surface=pickleball
                &kind=reservation
                &courts_for_pros=false

        which returns an array of 30-min slots, each tagged with:
          - `schedule`: human label like "5-5:30pm"
          - `seconds_from_midnight`: exact start time
          - `available`: bool — is any court free?
          - `shift`: "day" or "primetime" (pricing tier indicator)
          - `facility_schedule_id`: groups slots into pricing tiers

        Plus a `meta.specific_rules` block with booking constraints:
          - `playerBookingTimeStep`: slot length in seconds (1800 = 30min)
          - `minimum_time_slots_allowed_to_book`: min slots per booking
          - `max_consecutive_hours`: max booking duration
          - `amount_of_max_hours_prior_to_book`: how far ahead you can book

        Strategy: load /book/<slug> once to bootstrap auth cookies, then
        for each day, navigate to the date (which triggers the XHR) and
        consume the freshly-captured response. The first response also
        gives us the booking rules, which we attach to the ScrapeResult.

        Note: this endpoint shows AGGREGATE availability across all
        courts ("at least one court is free at HH:MM"). PBP's UI shows
        per-court breakdown only inside the VIEW CALENDAR overlay, which
        we ALSO scrape (when it opens) to attribute bookings to specific
        courts. The two data sources complement each other:
          - available_hours XHR  → per-slot bookable times + pricing tier
          - calendar overlay DOM → per-court booked blocks with type
        """
        url = f"{self.app_base_url}/book/{self.club_slug}"
        logger.info("Loading booking page: {}", url)

        try:
            await self._goto_with_retry(page, url)
            await page.wait_for_timeout(NETWORK_IDLE_MS)
            await self._wait_for_cloudflare(
                page,
                expected_selectors=[
                    "button:has-text('VIEW CALENDAR')",
                    "button:has-text('TUE'), button:has-text('WED')",
                    "main",
                ],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Booking page failed to load: {}", exc)
            self._notes.append(f"Booking page failed: {exc}")
            return []

        # Open the calendar overlay too (best-effort) — it gives us per-court
        # booked blocks alongside the API's aggregate availability.
        calendar_opened = await self._open_calendar_overlay(page)

        sessions: list[SessionRecord] = []

        for offset in range(self.number_of_days):
            day = self.start_date + timedelta(days=offset)
            logger.info("Booking day {}/{}: {}", offset + 1,
                        self.number_of_days, day.isoformat())

            xhr_checkpoint = len(self._raw_responses)

            # Navigate to the day. Click the date button on the strip.
            clicked = await self._click_date_button(page, day)
            if not clicked:
                advanced = await self._advance_date_strip(page)
                if advanced:
                    await page.wait_for_timeout(800)
                    clicked = await self._click_date_button(page, day)
            if not clicked:
                logger.warning("  → could not navigate to {}", day.isoformat())
                self._notes.append(
                    f"Date button not found for {day.isoformat()}"
                )
                continue

            await page.wait_for_timeout(NETWORK_IDLE_MS)

            # Save HTML snapshot of first booking page (for debug).
            if (self.dump_xhr and offset == 0
                    and not getattr(self, "_dumped_court_html", False)):
                try:
                    Path("debug").mkdir(exist_ok=True)
                    html = await page.content()
                    out = Path("debug") / "first_court_grid.html"
                    out.write_text(html, encoding="utf-8")
                    logger.debug("Dumped court grid HTML to {}", out)
                    self._dumped_court_html = True
                except Exception:
                    pass

            # PRIMARY DATA SOURCE: the available_hours XHR.
            day_xhrs = self._raw_responses[xhr_checkpoint:]
            slot_sessions = self._sessions_from_available_hours(
                day_xhrs, day,
            )

            # SECONDARY: per-court booked blocks from the calendar overlay,
            # if it opened. Adds court_number / session_type info to
            # complement the slot-level availability.
            booked_sessions: list[SessionRecord] = []
            if calendar_opened:
                booked_sessions = await self._extract_calendar_day(page, day)

            sessions.extend(slot_sessions)
            sessions.extend(booked_sessions)

            logger.info(
                "  → {} slot(s), {} per-court booking(s)",
                len(slot_sessions), len(booked_sessions),
            )

            if not slot_sessions and not booked_sessions and offset == 0:
                await self._dump_debug(page, f"booking_{day.isoformat()}")

            await self._polite_delay()

        return sessions

    def _sessions_from_available_hours(
        self, day_xhrs: list[dict], day: date,
    ) -> list[SessionRecord]:
        """
        Turn the /available_hours XHR payload into per-slot SessionRecords.
        Also captures the meta.specific_rules booking constraints on the
        first call, attaching them to self.booking_rules.

        Important: the FIRST /available_hours XHR fires automatically
        when /book/<slug> loads, BEFORE any date click. So we can't rely
        on `day_xhrs` being limited to post-click responses for day 0.
        Instead we look across ALL captured responses and pick the one
        whose `timestamp` query param matches `day` (it's the unix
        timestamp of midnight in the venue's timezone).
        """
        # Search ALL captured XHRs (not just the checkpoint slice) because
        # the first call fires on page load before our loop starts.
        candidates = []
        for entry in self._raw_responses:
            url = entry.get("url", "")
            if "/api/facilities/" not in url or "available_hours" not in url:
                continue
            # Parse the `timestamp=` query param; it's the unix timestamp
            # of the requested day at midnight local time.
            m = re.search(r"[?&]timestamp=(\d+)", url)
            if not m:
                # No timestamp — could be the first/default load. Treat
                # as a fallback candidate.
                candidates.append((None, entry))
                continue
            ts = int(m.group(1))
            try:
                # The timestamp is local-midnight, but it's expressed as
                # a unix epoch. We compare by date-of-the-day at the
                # venue's TZ. We don't have pytz; approximate by reading
                # ±12 hours from the timestamp and matching against `day`.
                ts_dates = {
                    datetime.fromtimestamp(ts + offset_h * 3600,
                                           tz=timezone.utc).date()
                    for offset_h in range(-14, 15)
                }
                if day in ts_dates:
                    candidates.append((ts, entry))
            except (OSError, OverflowError, ValueError):
                continue

        if not candidates:
            return []

        # Prefer entries whose timestamp matched; use the most recent
        # (highest ts) such entry. Falls back to the timestamp-less one.
        candidates.sort(key=lambda x: (x[0] is None, -(x[0] or 0)))
        entry = candidates[0][1]

        out: list[SessionRecord] = []
        url = entry.get("url", "")
        payload = entry.get("payload") or {}
        slots = payload.get("available_hours") or []
        if not slots:
            return []

        # Capture booking rules once.
        if self.booking_rules is None:
            meta = (payload.get("meta") or {})
            rules = (meta.get("specific_rules") or {})
            if rules:
                step_seconds = rules.get("playerBookingTimeStep")
                slot_length_min = (
                    int(step_seconds / 60)
                    if isinstance(step_seconds, (int, float)) else None
                )
                # IMPORTANT: `minimum_time_slots_allowed_to_book` is in
                # HOURS, not slots — confirmed by the on-page text
                # "Minimum playing time is: 1 hour" for value 1.0.
                # Likewise `max_consecutive_hours` is hours.
                min_hours = rules.get("minimum_time_slots_allowed_to_book")
                self.booking_rules = BookingRules(
                    slot_length_minutes=slot_length_min,
                    min_slots_to_book=None,  # we use min_hours directly now
                    max_consecutive_hours=rules.get(
                        "max_consecutive_hours"
                    ),
                    min_advance_hours=rules.get(
                        "amount_of_hours_prior_to_book"
                    ),
                    max_advance_hours=rules.get(
                        "amount_of_max_hours_prior_to_book"
                    ),
                    next_open_at=rules.get("nextOpenScheduleDateTime"),
                    raw={
                        **rules,
                        # Stash the parsed min hours for the BookingRules
                        # `.min_booking_minutes` property to read.
                        "_min_hours_parsed": min_hours,
                    },
                )
                if self.booking_rules.min_booking_minutes:
                    logger.info(
                        "Booking rules: slot={}min, min={}min, "
                        "max={}h, advance window={}h",
                        self.booking_rules.slot_length_minutes,
                        self.booking_rules.min_booking_minutes,
                        self.booking_rules.max_consecutive_hours,
                        self.booking_rules.max_advance_hours,
                    )

        slot_length_min = (
            self.booking_rules.slot_length_minutes
            if self.booking_rules else 30
        )

        for slot in slots:
            start_sec = slot.get("seconds_from_midnight")
            if not isinstance(start_sec, (int, float)):
                continue
            start_time = _seconds_to_hhmm(start_sec)
            end_time = _seconds_to_hhmm(start_sec + slot_length_min * 60)
            available = bool(slot.get("available"))
            in_waitlist = bool(slot.get("in_waitlist"))
            shift = slot.get("shift")
            status = (
                "Available" if available
                else ("Waitlist" if in_waitlist else "Unavailable")
            )
            out.append(SessionRecord(
                date=day.isoformat(),
                start_time=start_time,
                end_time=end_time,
                session_type="Court Slot",
                title=slot.get("schedule"),
                status=status,
                pricing_tier=shift,
                external_id=(
                    str(slot.get("facility_schedule_id"))
                    if slot.get("facility_schedule_id") else None
                ),
                source="xhr",
                raw=slot,
            ))
        logger.debug("Parsed {} slots from {}", len(out), url[:120])
        return out

    async def _open_calendar_overlay(self, page) -> bool:
        """
        Click the 'VIEW CALENDAR' button to open the schedule overlay.
        """
        clicked = await self._click_any(
            page,
            [
                "button:has-text('VIEW CALENDAR')",
                "button:has-text('View Calendar')",
                "a:has-text('VIEW CALENDAR')",
                "a:has-text('View Calendar')",
                "[aria-label*='calendar' i]",
            ],
            timeout_ms=5000,
            label="View Calendar",
        )
        if not clicked:
            return False
        # Wait for the overlay to render. The calendar table has a
        # 'Wednesday, May 13'-style heading; we wait for that.
        try:
            await page.wait_for_selector(
                "text=/(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,/",
                timeout=8000,
            )
            return True
        except Exception:
            # Some PBP builds use a different heading. Even if we can't
            # confirm, try to proceed — extraction will fail loudly later.
            await page.wait_for_timeout(2000)
            return True

    async def _read_calendar_current_date(self, page) -> Optional[date]:
        """
        Read the calendar overlay's current day from its 'Wednesday, May 13'
        heading. Returns a date or None.
        """
        try:
            heading = await page.evaluate(
                """
                () => {
                    // Grep the document for the weekday heading pattern.
                    const re = /(Monday|Tuesday|Wednesday|Thursday|Friday|"""
                """Saturday|Sunday),\\s+(\\w+)\\s+(\\d+)/;
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT
                    );
                    let n;
                    while ((n = walker.nextNode())) {
                        const m = (n.nodeValue || '').match(re);
                        if (m) return { month: m[2], day: parseInt(m[3]) };
                    }
                    return null;
                }
                """
            )
        except Exception:
            return None
        if not heading:
            return None
        try:
            # PBP doesn't print the year on the calendar heading, so we
            # assume the year of self.start_date — fine unless the window
            # straddles December → January.
            year = self.start_date.year
            return datetime.strptime(
                f"{heading['day']} {heading['month']} {year}", "%d %B %Y"
            ).date()
        except Exception:
            return None

    async def _step_calendar_to(
        self, page, current: date, target: date
    ) -> None:
        """
        Click the calendar's prev/next-day arrows N times to land on
        the target date.
        """
        delta = (target - current).days
        if delta == 0:
            return
        forward = delta > 0
        steps = abs(delta)
        # The arrows are typically labelled with → / ← icons or aria-label.
        forward_selectors = [
            "button[aria-label*='next' i]",
            "button[aria-label*='forward' i]",
            "button[aria-label*='Next day' i]",
            # The screenshot shows two arrow buttons in a row, with the
            # second one being 'forward'. As a last resort we'll pick
            # the second of two adjacent buttons.
        ]
        back_selectors = [
            "button[aria-label*='prev' i]",
            "button[aria-label*='back' i]",
            "button[aria-label*='Previous day' i]",
        ]
        selectors = forward_selectors if forward else back_selectors

        for _ in range(steps):
            clicked = await self._click_any(
                page, selectors, timeout_ms=3000,
                label=("Next day" if forward else "Prev day"),
            )
            if not clicked:
                # Try the JS fallback: find two arrow buttons in a row
                # (the calendar's nav cluster) and click the appropriate one.
                ok = await page.evaluate(
                    """
                    (forward) => {
                        const candidates = Array.from(
                            document.querySelectorAll('button')
                        ).filter(b => {
                            const t = (b.innerText || '').trim();
                            const al = (b.getAttribute('aria-label')
                                       || '').toLowerCase();
                            return t === '←' || t === '→'
                                   || t === '<' || t === '>'
                                   || al.includes('next')
                                   || al.includes('prev')
                                   || al.includes('back')
                                   || al.includes('forward');
                        });
                        if (candidates.length < 2) return false;
                        const target = forward
                            ? candidates[candidates.length - 1]
                            : candidates[0];
                        target.click();
                        return true;
                    }
                    """,
                    forward,
                )
                if not ok:
                    logger.debug("Date-step click failed (forward={}); "
                                 "calendar may not have advanced.", forward)
                    return
            await page.wait_for_timeout(400)

    async def _extract_calendar_day(self, page, day: date) -> list[SessionRecord]:
        """
        Pull reservations from the calendar overlay for the day currently
        in view. Tries three sources in order:

          1. data-react-props on any React component anywhere on the page.
             PBP often embeds the whole calendar's data as a single
             component prop (like ClinicStepperIndividualSesions for
             programs).
          2. Most-recent captured XHR JSON containing reservation-shaped
             items.
          3. DOM scrape of the visible reservation blocks.
        """
        sessions: list[SessionRecord] = []

        # Save the calendar HTML on the first day, for debug.
        if self.dump_xhr and not getattr(self, "_dumped_calendar_html", False):
            try:
                Path("debug").mkdir(exist_ok=True)
                html = await page.content()
                (Path("debug") / "first_calendar.html").write_text(
                    html, encoding="utf-8"
                )
                logger.debug("Dumped calendar HTML to debug/first_calendar.html")
                self._dumped_calendar_html = True
            except Exception:
                pass

        # PATH 1: react-props — try any component on the calendar overlay.
        try:
            props_list = await page.evaluate(
                """
                () => {
                    const els = Array.from(document.querySelectorAll(
                        '[data-react-class]'
                    ));
                    return els.map(el => ({
                        cls: el.getAttribute('data-react-class'),
                        props: el.getAttribute('data-react-props'),
                    }));
                }
                """
            )
            for entry in props_list or []:
                cls = entry.get("cls") or ""
                props_json = entry.get("props")
                if not props_json:
                    continue
                # Calendar / reservation components likely have names
                # containing 'Calendar', 'Reservation', 'Schedule'.
                if not re.search(r"calendar|reservation|schedule|day|grid",
                                 cls, re.IGNORECASE):
                    continue
                try:
                    props = json.loads(props_json)
                except Exception:
                    continue
                sessions.extend(
                    self._sessions_from_calendar_props(props, day)
                )
        except Exception as exc:
            logger.debug("react-props extraction failed: {}", exc)

        # PATH 2: scan the last batch of captured XHRs.
        if not sessions:
            for entry in reversed(self._raw_responses):
                payload = entry.get("payload")
                for item in _walk_json_for_sessions(payload):
                    rec = _coerce_session(
                        item, default_date=day,
                        default_session_type="Court Booking",
                    )
                    if rec and rec.date == day.isoformat():
                        sessions.append(rec)
                if sessions:
                    break

        # PATH 3: DOM scrape — pull the visible reservation blocks.
        if not sessions:
            try:
                blocks = await page.evaluate(
                    """
                    (isoDate) => {
                        // Look for the calendar table's body rows. PBP
                        // renders one row per court, with reservation
                        // blocks positioned across the time axis.
                        // Each block typically has a category (RESERVATION,
                        // OPEN PLAY, COACHING, NEW TO PICKLE, PERMANENT
                        // BOOKINGS) plus a title and a time range like
                        // '9:00 AM -10:00 AM'.
                        const rows = document.querySelectorAll(
                            'tr, [class*="row"]'
                        );
                        const out = [];
                        for (const row of rows) {
                            const courtCell = row.querySelector(
                                'th, [class*="court"]'
                            );
                            const courtName = courtCell
                                ? (courtCell.innerText || '').trim()
                                : null;
                            // Look for "category" labels inside the row
                            // (uppercase short strings) and the block they
                            // belong to.
                            const blocks = row.querySelectorAll(
                                '[class*="block"], [class*="event"], '
                                + '[class*="reservation"]'
                            );
                            for (const blk of blocks) {
                                const text = (blk.innerText || '').trim();
                                if (!text) continue;
                                out.push({ court: courtName, text });
                            }
                        }
                        return out;
                    }
                    """,
                    day.isoformat(),
                )
            except Exception:
                blocks = []
            for blk in blocks or []:
                rec = _parse_calendar_block_text(
                    blk.get("text") or "",
                    court_name=blk.get("court"),
                    day=day,
                )
                if rec:
                    sessions.append(rec)

        return sessions

    def _sessions_from_calendar_props(
        self, props: Any, day: date
    ) -> list[SessionRecord]:
        """
        Walk a React-component props payload for calendar-shaped items.
        Reuses the generic walker but tags everything as Court Booking
        and limits to `day`.
        """
        out: list[SessionRecord] = []
        for item in _walk_json_for_sessions(props):
            rec = _coerce_session(
                item, default_date=day,
                default_session_type="Court Booking",
            )
            if rec and rec.date == day.isoformat():
                out.append(rec)
        return out

    async def _scrape_date_picker_fallback(self, page) -> list[SessionRecord]:
        """
        If the calendar overlay can't be opened, harvest what we can
        from the time-slot picker: per-day, which 30-min slots are
        available (gray) vs unavailable (red). Less informative — we
        don't know WHAT is booked — but useful as a last resort.
        """
        sessions: list[SessionRecord] = []
        for offset in range(self.number_of_days):
            day = self.start_date + timedelta(days=offset)
            logger.info("Date-picker fallback for {}", day.isoformat())
            clicked = await self._click_date_button(page, day)
            if not clicked:
                continue
            await page.wait_for_timeout(NETWORK_IDLE_MS)
            # Look at every slot button on the page; the red ones are
            # unavailable.
            slots = await page.evaluate(
                """
                () => {
                    const btns = Array.from(
                        document.querySelectorAll("button, [role='button']")
                    );
                    return btns
                        .filter(b => /\\d{1,2}([:.\\-]\\d{0,2})?\\s*[AP]M/i
                                     .test(b.innerText || ''))
                        .map(b => ({
                            text: (b.innerText || '').trim(),
                            disabled: b.disabled || b.getAttribute(
                                'aria-disabled') === 'true',
                            cls: b.className || '',
                        }));
                }
                """
            )
            for s in slots or []:
                # Time range looks like "9-9:30AM" or "6:30-7AM".
                m = re.match(
                    r"^\s*(\d{1,2}(?::\d{2})?)\s*-\s*"
                    r"(\d{1,2}(?::\d{2})?)\s*([AP]M)",
                    s.get("text", ""),
                    re.IGNORECASE,
                )
                if not m:
                    continue
                status = ("Unavailable" if ("red" in s.get("cls", "").lower()
                                             or s.get("disabled"))
                          else "Available")
                sessions.append(SessionRecord(
                    date=day.isoformat(),
                    title=s.get("text"),
                    session_type="Court Slot",
                    status=status,
                    source="dom",
                    raw={"slot": s},
                ))
        return sessions

    async def _click_date_button(self, page, day: date) -> bool:
        """
        Click the date button matching `day` on the booking-grid SPA.

        PBP renders each date as a button with two stacked text lines:
        e.g. 'TUE' on top, '12' below. Same widget as the white-label
        step-flow UI from the screenshot.

        Returns True if a button was clicked, False if not found.
        """
        weekday = day.strftime("%a").upper()       # e.g. 'TUE'
        day_num = str(day.day)

        # We need a button whose text contains BOTH the weekday and the
        # day number. Naive `:has-text('TUE'):has-text('12')` will
        # incorrectly match a row containing both texts separately, so
        # we use a JS evaluate to find the precise element.
        clicked_sel = await page.evaluate(
            """
            ([weekday, dayNum]) => {
                const buttons = Array.from(document.querySelectorAll(
                    "button, [role='button'], div[class*='date']"
                ));
                for (const b of buttons) {
                    const txt = (b.innerText || '').toUpperCase()
                                    .replace(/\\s+/g, ' ').trim();
                    // Match e.g. 'TUE 12' or 'TUE\\n12' (collapsed above).
                    if (txt === `${weekday} ${dayNum}` ||
                        txt === `${weekday}${dayNum}` ||
                        (txt.includes(weekday) && txt.includes(dayNum)
                         && txt.length <= 12)) {
                        b.click();
                        return txt;
                    }
                }
                return null;
            }
            """,
            [weekday, day_num],
        )
        if clicked_sel:
            logger.debug("Clicked date button: {!r}", clicked_sel)
            return True
        return False

    async def _advance_date_strip(self, page) -> bool:
        """
        Click the 'forward' / 'next' arrow on the date-button strip to
        bring later dates into view. Returns True if a button was clicked.
        """
        clicked = await page.evaluate(
            """
            () => {
                // Common patterns for forward arrows: aria-label='next',
                // SVG icons with class names like 'chevron-right', or
                // text/symbol buttons containing '>' or '→'.
                const candidates = Array.from(document.querySelectorAll(
                    "button[aria-label*='next' i], "
                    + "button[aria-label*='forward' i], "
                    + "button:has(svg[class*='right' i]), "
                    + "button:has(svg[class*='forward' i])"
                ));
                for (const b of candidates) {
                    if (b.offsetParent !== null) {  // visible
                        b.click();
                        return b.outerHTML.slice(0, 100);
                    }
                }
                return null;
            }
            """
        )
        if clicked:
            logger.debug("Advanced date strip via: {}", clicked)
            return True
        return False

    async def _wait_for_cloudflare(
        self,
        page,
        max_seconds: int = 180,
        expected_selectors: Optional[list[str]] = None,
    ) -> bool:
        """
        Detect Cloudflare challenge pages and wait for them to clear.

        Cloudflare's challenge UI flickers — the checkbox can disappear for
        a moment while the JS is still running, before the actual
        `cf_clearance` cookie is issued. Detecting "UI is gone" therefore
        gives false positives.

        We instead consider the challenge cleared when BOTH:
          1. The `cf_clearance` cookie exists for the current domain, AND
          2. At least one of `expected_selectors` is visible on the page
             (or, if no selectors were given, the challenge markers are
             all absent for two consecutive polls).

        In headless mode we bail immediately with a clear error; the
        Turnstile checkbox can't be ticked without a real cursor.
        """
        challenge_markers = [
            "text=Verify you are human",
            "text=Performing security verification",
            "text=Checking your browser",
            "text=Just a moment",
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[title*='Cloudflare']",
            "#cf-challenge-running",
        ]

        async def _is_interstitial() -> bool:
            """
            The most reliable signal that we're on a Cloudflare challenge
            page: the document title is 'Just a moment...'. PBP's real
            pages have titles like 'Sign in to Playbypoint', 'Programs', etc.
            """
            try:
                title = await page.title()
                if title.strip().lower() in (
                    "just a moment...",
                    "just a moment…",
                    "attention required! | cloudflare",
                ):
                    return True
            except Exception:
                pass
            return False

        async def _challenge_visible() -> bool:
            # Title is the strongest signal.
            if await _is_interstitial():
                return True
            for sel in challenge_markers:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        return True
                except Exception:
                    continue
            return False

        async def _has_clearance_cookie() -> bool:
            try:
                cookies = await page.context.cookies()
                return any(c.get("name") == "cf_clearance" for c in cookies)
            except Exception:
                return False

        async def _expected_visible() -> bool:
            if not expected_selectors:
                return False
            for sel in expected_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        return True
                except Exception:
                    continue
            return False

        # Quick path: no challenge AND we already have clearance AND
        # the expected content is here.
        if not await _challenge_visible():
            if await _has_clearance_cookie() or await _expected_visible():
                return True
            # Otherwise fall through — page is loading.

        if self.headless and await _challenge_visible():
            logger.error(
                "Cloudflare challenge detected in HEADLESS mode. "
                "Re-run with `--no-headless` so you can solve the "
                "checkbox manually. The persistent profile keeps the "
                "clearance cookie for future runs."
            )
            await self._dump_debug(page, "cloudflare_challenge")
            return False

        logger.warning(
            "Cloudflare challenge detected. PLEASE CLICK THE "
            "'Verify you are human' CHECKBOX in the browser window if "
            "you see one. Waiting up to {}s …", max_seconds,
        )

        # Poll: we require BOTH challenge-UI absent AND
        # (clearance cookie present OR expected content visible),
        # and we require it to be stable for 2 consecutive polls so we
        # don't race the Cloudflare JS.
        elapsed = 0
        clean_streak = 0
        while elapsed < max_seconds:
            await page.wait_for_timeout(2000)
            elapsed += 2

            if await _challenge_visible():
                clean_streak = 0
                continue

            has_cookie = await _has_clearance_cookie()
            has_content = await _expected_visible()
            # Both signals required: the cookie alone is unreliable
            # (Cloudflare sometimes issues it for the challenge page itself
            # before the real page loads).
            if has_content and not await _is_interstitial():
                clean_streak += 1
                if clean_streak >= 2:
                    # Extra settle time for the post-challenge redirect
                    # to bring us back to the originally requested page.
                    await page.wait_for_timeout(3000)
                    logger.success(
                        "Cloudflare cleared after {}s "
                        "(cf_clearance={}, content={}).",
                        elapsed, has_cookie, has_content,
                    )
                    return True
            else:
                clean_streak = 0

        logger.error("Cloudflare challenge did not fully clear within {}s. "
                     "cf_clearance cookie present: {}",
                     max_seconds, await _has_clearance_cookie())
        await self._dump_debug(page, "cloudflare_timeout")
        return False

    async def _is_logged_in(self, page) -> bool:
        """
        Heuristic: PBP shows a 'Sign In' / 'Log In' link in the header when
        anonymous, and a profile avatar / 'Sign Out' link when authenticated.
        """
        try:
            # If a 'Sign Out' / 'Log Out' affordance exists anywhere, we're in.
            signout = await page.query_selector(
                "a[href*='sign_out'], a:has-text('Sign Out'), "
                "a:has-text('Log Out'), button:has-text('Sign Out')"
            )
            if signout:
                return True
            # Conversely, if a 'Sign In' link is still visible, we're out.
            signin = await page.query_selector(
                "a[href*='sign_in']:visible, a:has-text('Log In'):visible, "
                "a:has-text('Sign In'):visible"
            )
            return signin is None
        except Exception:
            return False

    async def _dump_debug(self, page, label: str) -> None:
        """Save a screenshot + HTML snapshot to help diagnose extraction misses."""
        out_dir = Path("debug")
        out_dir.mkdir(exist_ok=True)
        png = out_dir / f"{label}.png"
        html = out_dir / f"{label}.html"
        try:
            await page.screenshot(path=str(png), full_page=True)
            content = await page.content()
            html.write_text(content, encoding="utf-8")
            logger.warning("Saved debug snapshot: {} + {}", png, html)
            self._notes.append(f"Debug snapshot saved: {png}")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not save debug snapshot: {}", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _goto_with_retry(self, page, url: str) -> None:
        """Navigate with exponential backoff. Cloudflare sometimes 503s."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with attempt:
                resp = await page.goto(url, wait_until="domcontentloaded",
                                       timeout=30000)
                if resp and resp.status >= 500:
                    raise RuntimeError(f"HTTP {resp.status} on {url}")

    async def _polite_delay(self) -> None:
        delay = random.uniform(1.2, 2.8)
        await asyncio.sleep(delay)

    async def _safe_text(self, page, selector: str) -> Optional[str]:
        try:
            el = await page.query_selector(selector)
            if not el:
                return None
            return (await el.inner_text()).strip() or None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _sessions_from_xhrs(
        self,
        *,
        default_date: Optional[date] = None,
        default_title: Optional[str] = None,
        default_session_type: Optional[str] = None,
        program_url: Optional[str] = None,
        default_price: Optional[str] = None,
    ) -> list[SessionRecord]:
        """
        Walk the captured XHR payloads and pull out anything that looks like
        a session/reservation. PlayByPoint's JSON shape varies by endpoint,
        so we duck-type aggressively rather than relying on a fixed schema.
        """
        sessions: list[SessionRecord] = []
        for entry in self._raw_responses:
            payload = entry.get("payload")
            for item in _walk_json_for_sessions(payload):
                rec = _coerce_session(
                    item,
                    default_date=default_date,
                    default_title=default_title,
                    default_session_type=default_session_type,
                    program_url=program_url,
                    default_price=default_price,
                )
                if rec:
                    sessions.append(rec)
        return sessions

    def _parse_session_row_text(
        self,
        text: str,
        *,
        program_url: str,
        default_title: Optional[str],
        default_price: Optional[str],
    ) -> Optional[SessionRecord]:
        """
        Best-effort regex parse of a free-text session row like:
            "Wed 22 May · 6:00 PM – 7:30 PM · 2/8 spots · $20.00"
        """
        if not text:
            return None

        date_match = re.search(
            r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"(?:\s+(\d{4}))?",
            text,
        )
        time_match = re.search(
            r"(\d{1,2}:\d{2}\s*[AP]M)\s*[–-]\s*(\d{1,2}:\d{2}\s*[AP]M)",
            text,
            re.IGNORECASE,
        )
        spots_match = re.search(r"(\d+)\s*/\s*(\d+)\s*spots?", text, re.IGNORECASE)
        price_match = re.search(r"\$\s?\d+(?:\.\d{2})?", text)
        skill_match = re.search(
            r"(beginner|intermediate|advanced|2\.\d|3\.\d|4\.\d|5\.\d)",
            text,
            re.IGNORECASE,
        )

        iso_date = None
        if date_match:
            day_num, month_abbr, year = date_match.groups()
            year = int(year) if year else self.start_date.year
            try:
                iso_date = datetime.strptime(
                    f"{day_num} {month_abbr} {year}", "%d %b %Y"
                ).date().isoformat()
            except ValueError:
                iso_date = None

        start_time = end_time = None
        if time_match:
            try:
                start_time = datetime.strptime(
                    time_match.group(1).strip().upper().replace(" ", ""),
                    "%I:%M%p",
                ).strftime("%H:%M")
                end_time = datetime.strptime(
                    time_match.group(2).strip().upper().replace(" ", ""),
                    "%I:%M%p",
                ).strftime("%H:%M")
            except ValueError:
                pass

        spots_available = max_spots = None
        if spots_match:
            spots_taken = int(spots_match.group(1))
            max_spots = int(spots_match.group(2))
            spots_available = max(max_spots - spots_taken, 0)

        # If we have no date AND no time, this row isn't a session.
        if not iso_date and not start_time:
            return None

        return SessionRecord(
            date=iso_date or self.start_date.isoformat(),
            start_time=start_time,
            end_time=end_time,
            session_type=_guess_session_type(program_url, default_title),
            title=default_title,
            skill_level=skill_match.group(0) if skill_match else None,
            spots_available=spots_available,
            max_spots=max_spots,
            price=(price_match.group(0) if price_match else default_price),
            booking_link=program_url,
            status=("Available" if spots_available and spots_available > 0
                    else ("Full" if max_spots else None)),
            source="dom",
            raw={"text": text},
        )


# ---------------------------------------------------------------------------
# Free helpers (top-level so they're easy to unit test)
# ---------------------------------------------------------------------------


def _parse_calendar_block_text(
    text: str,
    court_name: Optional[str],
    day: date,
) -> Optional[SessionRecord]:
    """
    Parse the inner text of a reservation block from PBP's calendar
    overlay. Observed shapes from the screenshot:

        "RESERVATION\\n6:00 AM -8:00 AM"
        "OPEN PLAY\\nBeginner - Social Open Play\\n10:00 AM -12:00 PM"
        "PERMANENT BOOKINGS\\nPrivate Event\\n9:00 AM -10:00 AM"
        "COACHING\\nCoaching Clinic ...\\n2:00 PM -3:00 PM"
        "NEW TO PICKLE\\nLearn To Play Pic...\\n1:00 PM -2:00 PM"
        "SAHIL DANG\\n12:00 PM -1:00 PM"     ← teacher-named block

    The first line is the CATEGORY (in caps); middle lines are the title
    (may include a teacher's name); the last line is the time range.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    # Find the time-range line — it's the one matching a HH:MM AM/PM
    # range pattern.
    time_match = None
    time_line_idx = None
    for idx, ln in enumerate(lines):
        m = re.search(
            r"(\d{1,2}(?::\d{2})?)\s*([AP]M)?\s*[-–]\s*"
            r"(\d{1,2}(?::\d{2})?)\s*([AP]M)",
            ln, re.IGNORECASE,
        )
        if m:
            time_match = m
            time_line_idx = idx
            break
    if not time_match:
        return None

    # Convert to HH:MM 24h.
    def _to_24h(time_part: str, am_pm_part: Optional[str]) -> Optional[str]:
        try:
            if ":" in time_part:
                hh, mm = time_part.split(":")
            else:
                hh, mm = time_part, "00"
            hh_i = int(hh)
            mm_i = int(mm)
        except ValueError:
            return None
        if am_pm_part:
            ap = am_pm_part.upper()
            if ap == "PM" and hh_i != 12:
                hh_i += 12
            elif ap == "AM" and hh_i == 12:
                hh_i = 0
        return f"{hh_i:02d}:{mm_i:02d}"

    start_ap = time_match.group(2) or time_match.group(4)
    end_ap = time_match.group(4)
    start_time = _to_24h(time_match.group(1), start_ap)
    end_time = _to_24h(time_match.group(3), end_ap)

    # First line is the category. PBP categories tend to be ALL CAPS.
    category = lines[0] if lines[0].isupper() else None
    # Title: lines between category and time-line (or whatever's left).
    title_lines = []
    for idx, ln in enumerate(lines):
        if idx == 0 and category:
            continue
        if idx == time_line_idx:
            continue
        title_lines.append(ln)
    title = " ".join(title_lines).strip() or None

    # Map category text to our session_type vocabulary.
    session_type = "Court Booking"
    if category:
        c = category.lower()
        if "open" in c and "play" in c:
            session_type = "Open Play"
        elif "coach" in c:
            session_type = "Clinic"
        elif "new to" in c:
            session_type = "Clinic"
        elif "permanent" in c:
            session_type = "Permanent Booking"
        elif "reservation" in c:
            session_type = "Court Booking"
        # Otherwise the category text itself is meaningful (e.g.
        # "SAHIL DANG" = teacher name); leave as Court Booking but use
        # it as the title.
        elif title is None:
            title = category.title()

    return SessionRecord(
        date=day.isoformat(),
        start_time=start_time,
        end_time=end_time,
        court_name=court_name,
        session_type=session_type,
        title=title or category,
        status="Booked",
        source="dom",
        raw={"text": text, "category": category},
    )


def _seconds_to_hhmm(seconds: Optional[float]) -> Optional[str]:
    """
    Convert seconds-since-midnight (PBP's hour_start/hour_end) to HH:MM.

    Allows 24*3600 (== 86400, midnight at end of day) since PBP's last
    slot of the day ends at exactly that. We render it as '24:00'.
    """
    if seconds is None:
        return None
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    if total < 0 or total > 24 * 3600:
        return None
    if total == 24 * 3600:
        return "24:00"
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _hhmm(value: Optional[str]) -> Optional[str]:
    """Coerce assorted time strings into HH:MM, or None."""
    if not value:
        return None
    # ISO timestamp?
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M")
    except (ValueError, TypeError):
        pass
    # Already HH:MM?
    m = re.match(r"^(\d{1,2}):(\d{2})", value)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def _walk_json_for_sessions(node: Any) -> Iterable[dict]:
    """
    Recursively yield dicts that look like session/reservation records.
    Heuristic: dict with at least one of {start_time, starts_at, start_at,
    start, date} AND at least one of {price, court, spots, title, name}.
    """
    if isinstance(node, dict):
        keys = set(node.keys())
        if (keys & {"start_time", "starts_at", "start_at", "start", "date",
                    "scheduled_at"}) and (
            keys & {"price", "court", "court_id", "spots", "title", "name",
                    "program_id", "session_id", "id"}
        ):
            yield node
        for v in node.values():
            yield from _walk_json_for_sessions(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_json_for_sessions(v)


def _coerce_session(
    item: dict,
    *,
    default_date: Optional[date] = None,
    default_title: Optional[str] = None,
    default_session_type: Optional[str] = None,
    program_url: Optional[str] = None,
    default_price: Optional[str] = None,
) -> Optional[SessionRecord]:
    """Turn a duck-typed PBP session dict into a SessionRecord."""
    start_raw = (item.get("start_time") or item.get("starts_at")
                 or item.get("start_at") or item.get("start")
                 or item.get("scheduled_at"))
    end_raw = (item.get("end_time") or item.get("ends_at")
               or item.get("end_at") or item.get("end"))

    iso_date = None
    start_time = None
    if start_raw:
        try:
            dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            iso_date = dt.date().isoformat()
            start_time = dt.strftime("%H:%M")
        except ValueError:
            pass
    if not iso_date:
        iso_date = (item.get("date")
                    or (default_date.isoformat() if default_date else None))
    if not iso_date:
        return None

    end_time = None
    if end_raw:
        try:
            end_time = datetime.fromisoformat(
                str(end_raw).replace("Z", "+00:00")
            ).strftime("%H:%M")
        except ValueError:
            pass

    title = item.get("title") or item.get("name") or default_title
    court = item.get("court") or item.get("court_name")
    court_num = item.get("court_number") or item.get("court_id")

    spots_available = item.get("spots_available") or item.get("available_spots")
    max_spots = item.get("max_spots") or item.get("capacity") or item.get("max")

    price = item.get("price") or item.get("amount") or default_price
    if isinstance(price, (int, float)):
        price = f"${price:.2f}"

    status = item.get("status")
    if not status and isinstance(spots_available, int) and isinstance(max_spots, int):
        status = "Full" if spots_available == 0 else "Available"

    return SessionRecord(
        date=iso_date,
        start_time=start_time,
        end_time=end_time,
        court_number=str(court_num) if court_num is not None else None,
        court_name=str(court) if court else None,
        session_type=(default_session_type
                      or _guess_session_type(program_url, title)
                      or "Unknown"),
        title=title,
        skill_level=item.get("skill_level") or item.get("level"),
        spots_available=spots_available,
        max_spots=max_spots,
        price=str(price) if price is not None else None,
        booking_link=item.get("url") or item.get("booking_url") or program_url,
        external_id=str(item.get("id")) if item.get("id") else None,
        status=status,
        source="xhr",
        raw=item,
    )


def _normalise_category(category: Optional[str]) -> str:
    """
    Map PBP's `category` field to our session_type vocabulary.
    Observed PBP values: 'Open Play', 'Coaching', 'Social Event',
    'DUPR Session', 'New to Pickle'.
    """
    if not category:
        return "Program"
    c = category.strip().lower()
    if "open" in c and "play" in c:
        return "Open Play"
    if "coach" in c or "clinic" in c:
        return "Clinic"
    if "league" in c or "round robin" in c or "dupr" in c:
        return "League"
    if "social" in c:
        return "Social Event"
    if "new to" in c or "intro" in c or "beginner" in c:
        return "Clinic"
    return category.strip()


def _guess_session_type(url: Optional[str], title: Optional[str]) -> str:
    """Classify a session from its URL slug or title."""
    haystack = " ".join(filter(None, [url or "", title or ""])).lower()
    if "open" in haystack and "play" in haystack:
        return "Open Play"
    if "clinic" in haystack or "intro" in haystack:
        return "Clinic"
    if "league" in haystack:
        return "League"
    if "lesson" in haystack or "coach" in haystack:
        return "Coaching"
    if "court" in haystack or "book" in haystack:
        return "Court Booking"
    return "Program"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_json(result: ScrapeResult, path: Path) -> None:
    payload = {
        **{k: v for k, v in asdict(result).items() if k != "sessions"
           and k != "raw_responses" and k != "booking_rules"},
        "booking_rules": (
            asdict(result.booking_rules) if result.booking_rules else None
        ),
        "sessions": [asdict(s) for s in result.sessions],
        "raw_response_count": len(result.raw_responses),
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.success("Wrote {} sessions to {}", len(result.sessions), path)


def print_table(result: ScrapeResult) -> None:
    console = Console()

    # Show booking rules at the top if we captured them.
    if result.booking_rules:
        br = result.booking_rules
        lines = []
        if br.min_booking_minutes:
            lines.append(f"Min booking: [bold]{br.min_booking_minutes} min[/]")
        if br.max_consecutive_hours:
            lines.append(f"Max booking: [bold]{br.max_consecutive_hours}h[/]")
        if br.slot_length_minutes:
            lines.append(f"Slot length: [bold]{br.slot_length_minutes} min[/]")
        if br.max_advance_hours:
            days_ahead = int(br.max_advance_hours / 24)
            lines.append(
                f"Bookable up to: [bold]{days_ahead} days ahead[/]"
            )
        if lines:
            console.print(
                "[bold cyan]Court booking rules:[/]  " + "  ·  ".join(lines)
            )

    if not result.sessions:
        console.print("[yellow]No sessions extracted.[/yellow]")
        for note in result.notes:
            console.print(f"  [dim]• {note}[/dim]")
        return

    table = Table(
        title=f"{result.club_name}  ·  "
              f"{result.start_date} to {result.end_date}",
        show_lines=False,
    )
    for col in ["Date", "Start", "End", "Type", "Title",
                "Court", "Level", "Spots", "Price", "Tier", "Status"]:
        min_w = 10 if col == "Date" else 1
        table.add_column(col, overflow="fold", min_width=min_w)

    # Sort: date, then start_time
    for s in sorted(
        result.sessions,
        key=lambda x: (x.date or "", x.start_time or ""),
    ):
        spots = ""
        if s.spots_available is not None and s.max_spots is not None:
            spots = f"{s.spots_available}/{s.max_spots}"
        elif s.spots_available is not None:
            spots = str(s.spots_available)
        table.add_row(
            s.date or "",
            s.start_time or "",
            s.end_time or "",
            s.session_type or "",
            (s.title or "")[:40],
            s.court_name or s.court_number or "",
            s.skill_level or "",
            spots,
            s.price or "",
            s.pricing_tier or "",
            s.status or "",
        )
    console.print(table)
    if result.notes:
        console.print("\n[bold]Notes:[/bold]")
        for note in result.notes:
            console.print(f"  • {note}")


# ---------------------------------------------------------------------------
# FAST PATH — direct HTTP client against PlayByPoint's internal JSON APIs
# ---------------------------------------------------------------------------
#
# The Playwright scraper above is reliable but slow (~60-90s for 7 days).
# After studying the captured XHRs, we know all the endpoints PBP uses
# internally. We can hit them directly via httpx in parallel using the
# cookies harvested by one Playwright login, dropping the full scrape to
# under 5 seconds.
#
# Endpoints (all on https://app.playbypoint.com, all require auth):
#
#   GET /api/users/<uid>/current_facility
#       → {id, name}
#
#   GET /api/users/preferred_facilities?user_id=<uid>
#       → {facilities: [{id, name}, ...]}
#
#   GET /api/facilities/<fid>/court_types?kind=reservation
#       → [{id, surface, surface_name, rating_provider}]
#
#   GET /api/facilities/<fid>/available_hours
#       ?timestamp=<unix>&surface=pickleball&kind=reservation
#       &courts_for_pros=false
#       → {available_hours: [{schedule, shift, available, ...}, ...],
#          meta: {specific_rules: {playerBookingTimeStep, ...}}}
#       NOTE: timestamp is unix-seconds for midnight on the queried day,
#       in the venue's local timezone.
#
#   GET /api/facilities/<fid>/available_courts
#       ?date=<unix>&surface=pickleball
#       &start_hour=<sec>&hour_end=<sec>&kind=reservation
#       → [{id, name, surface, is_parent}, ...]
#       Lists which specific courts are free for a given time-window.
#
#   GET /api/courts/<court_id>/price
#       ?date=<unix>&admin_book=false
#       &hour_start=<sec>&hour_end=<sec>&players=1
#       &reservation_type=1&payment_method=card
#       → {total: {...}, prices_per_user: [{price: {
#              reservation_fare, total_hours,
#              shift_prices: [{shift, price, picked}], ...
#          }}]}
#       Per-court pricing. shift_prices[0].price is the HOURLY rate.
#       `picked` is the fraction of an hour the booking covers.
#
#   GET /api/public/clinics?search=&facility_id=<fid>   (NB: facility_id
#       is needed when called from app.playbypoint.com instead of the
#       white-label club domain)
#       → {clinics: [...]}  — program catalog (already used)

PBP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class PlayByPointAPI:
    """
    Async HTTP client for PlayByPoint's internal JSON APIs.

    Auth: pass in a cookie-jar (typically harvested from a Playwright
    session that completed login). The `_paybycourt_session` cookie is
    the critical one; `cf_clearance` keeps Cloudflare happy.

    TLS fingerprinting note
    -----------------------
    Cloudflare blocks plain httpx with 403 even when the right cookies
    are present, because httpx's TLS handshake has a Python/OpenSSL
    fingerprint (JA3) that doesn't match Chrome's. The cookies are
    issued FOR a Chrome fingerprint — present them from any other
    client and Cloudflare's bot-management rules trip.

    We use `curl_cffi` which wraps libcurl with browser TLS profiles:
    its `impersonate='chrome120'` (or similar) makes the handshake
    byte-identical to real Chrome. Cookies + matching fingerprint = pass.

    Falls back to httpx if curl_cffi isn't installed, but you'll
    likely see 403s in that case — install it:
        pip install curl_cffi
    """

    def __init__(
            self,
            *,
            cookies: dict[str, str],
            app_base_url: str = APP_BASE_URL,
            club_slug: str = DEFAULT_CLUB_SLUG,
            user_agent: str = PBP_USER_AGENT,
            timeout: float = 15.0,
            rate_limit: float = 0.05,
            proxy: Optional[str] = None,
    ) -> None:
        self.app_base_url = app_base_url.rstrip("/")
        self.club_slug = club_slug
        # PBP's API responds 401 without these standard XHR headers.
        # The Origin and Referer headers are checked server-side.
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-AU,en;q=0.9",
            "Origin": self.app_base_url,
            "Referer": f"{self.app_base_url}/home",
            "X-Requested-With": "XMLHttpRequest",
        }
        self._cookies = cookies
        self._timeout = timeout
        self._user_id: Optional[int] = None  # discovered at runtime
        self._proxy = proxy

        # Try curl_cffi first (browser-impersonating TLS).
        self._impl = "httpx"
        self._client = None
        try:
            from curl_cffi.requests import AsyncSession  # type: ignore
            # 'chrome' alias auto-picks a recent Chrome profile.
            session_kwargs = dict(
                base_url=self.app_base_url,
                headers=self._headers,
                cookies=self._cookies,
                timeout=timeout,
                impersonate="chrome",
            )
            if proxy:
                session_kwargs["proxies"] = {"https": proxy, "http": proxy}
            self._client = AsyncSession(**session_kwargs)
            self._impl = "curl_cffi"
            logger.debug("PBP API client using curl_cffi (chrome TLS).")
        except ImportError:
            import httpx  # type: ignore
            self._client = httpx.AsyncClient(
                base_url=self.app_base_url,
                headers=self._headers,
                cookies=self._cookies,
                timeout=timeout,
            )
            logger.warning(
                "curl_cffi not installed — falling back to httpx. "
                "Cloudflare will probably 403 the API calls. "
                "Run: pip install curl_cffi"
            )

        # Rate-limit between requests so we don't hammer the API.
        self._rate_limit = rate_limit
        self._last_request_at = 0.0

    async def aclose(self) -> None:
        if self._impl == "curl_cffi":
            await self._client.close()
        else:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def _get_json(
        self, path: str, params: Optional[dict] = None,
        referer: Optional[str] = None,
    ) -> Any:
        """GET with rate-limit + retry + JSON decode."""
        # Polite pacing.
        now = asyncio.get_event_loop().time()
        wait = self._rate_limit - (now - self._last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = asyncio.get_event_loop().time()

        # Build per-request headers (override Referer if specified).
        req_headers = {}
        if referer:
            req_headers["Referer"] = referer

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with attempt:
                resp = await self._client.get(
                    path, params=params,
                    headers=req_headers if req_headers else None,
                )
                status = resp.status_code
                if status == 401:
                    raise PermissionError(
                        f"401 Unauthorized for {path}. Session cookies may "
                        "have expired — re-run with --mode=reliable to "
                        "refresh, or set --mode=hybrid for auto-refresh."
                    )
                if status == 403:
                    raise PermissionError(
                        f"403 Forbidden for {path}. Almost certainly "
                        "Cloudflare blocking the request (TLS fingerprint "
                        "mismatch). Install curl_cffi: "
                        "pip install curl_cffi"
                    )
                if status >= 400:
                    raise RuntimeError(
                        f"HTTP {status} for {path}: "
                        f"{resp.text[:200] if hasattr(resp, 'text') else ''}"
                    )
                return resp.json()
        return None  # unreachable

    async def _post_json(
        self, url: str, json_body: dict,
        headers: Optional[dict] = None,
    ) -> Any:
        """POST JSON with rate-limit + retry."""
        now = asyncio.get_event_loop().time()
        wait = self._rate_limit - (now - self._last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = asyncio.get_event_loop().time()

        import json as _json
        body_str = _json.dumps(json_body, default=str)

        req_headers = {**(headers or {})}

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(2),
            wait=wait_exponential(multiplier=1, min=1, max=5),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with attempt:
                if self._impl == "curl_cffi":
                    resp = await self._client.post(
                        url, data=body_str, headers=req_headers,
                    )
                else:
                    resp = await self._client.post(
                        url, content=body_str, headers=req_headers,
                    )
                status = resp.status_code
                if status >= 400:
                    raise RuntimeError(
                        f"HTTP {status} for POST {url}: "
                        f"{resp.text[:300] if hasattr(resp, 'text') else ''}"
                    )
                return resp.json()
        return None

    async def _get_raw(self, url: str) -> Optional[str]:
        """GET and return raw text (for scraping CSRF tokens etc)."""
        now = asyncio.get_event_loop().time()
        wait = self._rate_limit - (now - self._last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = asyncio.get_event_loop().time()

        try:
            resp = await self._client.get(url)
            if resp.status_code < 400:
                return resp.text
        except Exception as exc:
            logger.debug("_get_raw failed for {}: {}", url, exc)
        return None

    # ----- facility search -----------------------------------------------

    async def current_user(self) -> Optional[dict]:
        """
        Discover the currently logged-in user from the session.

        Tries multiple endpoints to resolve user info:
          1. GET /api/users/current → {id, email, name, ...}
          2. Falls back to parsing user_id from cookie-dependent endpoints.

        Returns dict with at least {id, email, name} or None.
        """
        for path in ["/api/users/current", "/api/users/me"]:
            try:
                data = await self._get_json(path, params={})
                if isinstance(data, dict) and data.get("id"):
                    self._user_id = data["id"]
                    logger.info(
                        "Current user: {} (id={}, email={})",
                        data.get("name"), data.get("id"), data.get("email"),
                    )
                    return data
            except Exception:
                continue

        # Fallback: try current_facility which often returns user context.
        if self._user_id:
            try:
                data = await self._get_json(
                    f"/api/users/{self._user_id}/current_facility",
                    params={},
                )
                if isinstance(data, dict):
                    return {"id": self._user_id, "facility": data}
            except Exception:
                pass

        return None

    async def saved_cards(self) -> list[dict]:
        """
        Get the user's saved payment cards.

        Endpoint: GET /api/cards
        Returns list of {id, last4, brand, exp_month, exp_year, is_default}.
        """
        try:
            data = await self._get_json(
                "/api/cards", params={},
                referer=f"{self.app_base_url}/home",
            )
            cards = data if isinstance(data, list) else (data or {}).get("cards") or []
            if cards:
                logger.info(
                    "Found {} saved card(s): {}",
                    len(cards),
                    ", ".join(
                        f"{c.get('brand','?')} •••{c.get('last4','?')}"
                        for c in cards
                    ),
                )
            return cards
        except Exception as exc:
            logger.debug("Could not fetch saved cards: {}", exc)
            return []

    async def user_balance(self, facility_id: int) -> Optional[dict]:
        """
        Get the user's prepaid/account balance at a facility.

        Endpoint: GET /api/users/{user_id}/balance/{facility_id}
        """
        if not self._user_id:
            return None
        try:
            return await self._get_json(
                f"/api/users/{self._user_id}/balance/{facility_id}",
                params={},
            )
        except Exception:
            return None

    async def search_facilities(self, query: str) -> list[dict]:
        """
        Search for venues on PlayByPoint.

        Endpoint: GET /api/facilities?q=<query>

        Returns a list of facilities, each with:
          - id: facility ID (e.g. 597)
          - name: "The Jar | South Melbourne"
          - city: "Melbourne"
          - court_number: 4
          - surface_list: "Pickleball"
          - url: "/f/nplpickleball"
          - book_url: "/book/nplpickleball"  (slug = last segment)
          - allow_online_booking: true
          - accepts_reservations_from_user: true
          - amenities: ["Lights", "Pro shop"]
          - average_rating: 0.0

        No authentication required — this is a public search endpoint
        (though it does go through Cloudflare, so curl_cffi is needed).
        """
        data = await self._get_json(
            "/api/facilities",
            params={"q": query},
            referer=f"{self.app_base_url}/home",
        )
        return (data or {}).get("facilities") or []

    async def recommended_facilities(self) -> list[dict]:
        """
        Get recommended/nearby facilities (the "Explore" page).

        Endpoint: GET /api/recommendations/facilities?white_label_facility_id=
        """
        data = await self._get_json(
            "/api/recommendations/facilities",
            params={"white_label_facility_id": ""},
            referer=f"{self.app_base_url}/home",
        )
        return (data or {}).get("facilities") or []

    # ----- discovery -----------------------------------------------------

    async def current_facility(self, user_id: int) -> dict:
        return await self._get_json(
            f"/api/users/{user_id}/current_facility"
        )

    async def preferred_facilities(self, user_id: int) -> list[dict]:
        data = await self._get_json(
            "/api/users/preferred_facilities",
            params={"user_id": user_id},
        )
        return (data or {}).get("facilities") or []

    async def court_types(self, facility_id: int) -> list[dict]:
        return await self._get_json(
            f"/api/facilities/{facility_id}/court_types",
            params={"kind": "reservation"},
        )

    # ----- per-day availability ----------------------------------------

    async def available_hours(
        self, facility_id: int, day: date, surface: str = "pickleball",
    ) -> dict:
        """Return {available_hours: [...], meta: {...}} for `day`."""
        ts = _local_midnight_unix(day)
        return await self._get_json(
            f"/api/facilities/{facility_id}/available_hours",
            params={
                "timestamp": ts,
                "surface": surface,
                "kind": "reservation",
                "courts_for_pros": "false",
            },
        )

    async def available_courts(
        self,
        facility_id: int,
        day: date,
        start_seconds: int,
        end_seconds: int,
        surface: str = "pickleball",
    ) -> list[dict]:
        """Return the list of courts free for a given time window."""
        ts = _local_midnight_unix(day)
        return await self._get_json(
            f"/api/facilities/{facility_id}/available_courts",
            params={
                "date": ts,
                "surface": surface,
                "start_hour": start_seconds,
                "hour_end": end_seconds,
                "kind": "reservation",
            },
        )

    async def court_price(
        self,
        court_id: int,
        day: date,
        start_seconds: int,
        end_seconds: int,
        players: int = 1,
        reservation_type: int = 1,
        user_id: Optional[int] = None,
    ) -> dict:
        """
        Return the pricing payload for one court / one time window.

        HAR-verified params (2026-05-13 booking session):
          date=<midnight_unix>  (NOT current time — HAR shows 1778767200)
          admin_book=false
          hour_start=<seconds>
          hour_end=<seconds>
          players_reservation_type=1
          payment_method=card
          user_ids[]=<user_id>        ← CRITICAL: not "players"
          kind=reservation
          user_who_is_paying=<user_id>
          users_fees[player0][fees][]=
          coupon_code=

        Without user_ids[] and user_who_is_paying, PBP returns
        affiliation=null and all prices as $0.
        """
        ts = _local_midnight_unix(day)
        params = {
            "date": ts,
            "admin_book": "false",
            "hour_start": start_seconds,
            "hour_end": end_seconds,
            "players_reservation_type": reservation_type,
            "payment_method": "card",
            "kind": "reservation",
            "coupon_code": "",
        }
        # The user_ids[] param is what makes pricing work.
        uid = user_id or self._user_id
        if uid:
            params["user_ids[]"] = uid
            params["user_who_is_paying"] = uid
            params["users_fees[player0][fees][]"] = ""
        else:
            # Fallback: old-style players param (returns $0 but won't error)
            params["players"] = players

        logger.debug(
            "court_price: court={} user_id={} hour_start={} hour_end={}",
            court_id, uid, start_seconds, end_seconds,
        )
        return await self._get_json(
            f"/api/courts/{court_id}/price",
            params=params,
            referer=f"{self.app_base_url}/book/{self.club_slug}",
        )

    async def book_court(
        self,
        court_id: int,
        day: date,
        start_seconds: int,
        end_seconds: int,
        user_id: Optional[int] = None,
        reservation_type: int = 1,
        payment_method: str = "accounts_receivable",
        payment_intent_id: str = "",
        public_game: bool = False,
        min_ntrp: float = 1,
        max_ntrp: float = 7,
        ntrp_verified: bool = False,
        dry_run: bool = True,
    ) -> dict:
        """
        Book a court via POST /api/courts/{court_id}/booking_player.

        HAR-verified payload (2026-05-13 booking session):

            {
              "reservation": {
                "date": "2026-05-15",
                "hour_start": 50400,
                "hour_end": 54000,
                "reservation_type": 1,
                "public_game": false,
                "min_ntrp": 1, "max_ntrp": 7,
                "kind": "reservation",
                "ntrp_verified": false
              },
              "payment": {
                "method": "accounts_receivable",
                "payment_intent_id": "",
                "card_details": {},
                "coupon": {"code": ""},
                "moment": "now"
              },
              "user_ids": [1973346],
              ...
            }

        Required headers:
          - X-CSRF-Token (scraped from page meta tag)
          - X-Requested-With: XMLHttpRequest
          - Content-Type: application/json
          - Referer: {app_base_url}/book/{club_slug}

        Payment methods seen in HAR:
          - "card"                 → Stripe card payment
          - "prepaid"              → prepaid balance
          - "accounts_receivable"  → charge to club account

        Args:
            dry_run: If True (default), returns the payload that WOULD be
                     sent without actually POSTing. Set to False to execute.
        """
        uid = user_id or self._user_id
        if not uid:
            raise ValueError(
                "user_id is required for booking. Run the scraper once "
                "to discover it, or pass --user-id explicitly."
            )

        payload = {
            "reservation": {
                "date": day.isoformat(),
                "hour_start": start_seconds,
                "hour_end": end_seconds,
                "reservation_type": reservation_type,
                "public_game": public_game,
                "min_ntrp": min_ntrp,
                "max_ntrp": max_ntrp,
                "kind": "reservation",
                "ntrp_verified": ntrp_verified,
            },
            "payment": {
                "method": payment_method,
                "payment_intent_id": payment_intent_id,
                "card_details": {},
                "coupon": {"code": ""},
                "moment": "now",
            },
            "user_ids": [uid],
            "user_excluded_ids": [],
            "user_ids_guest_names": {
                "player0": {"name": None},
            },
            "reservation_fees": [],
            "users_fees": {
                "player0": {"fees": [None]},
            },
            "auto_fill_courts": False,
            "free_fare_players": [],
            "guest_pass_users": [],
        }

        if dry_run:
            logger.info(
                "DRY RUN — would POST /api/courts/{}/booking_player "
                "with payload:\n{}",
                court_id,
                json.dumps(payload, indent=2, default=str),
            )
            return {"dry_run": True, "payload": payload, "court_id": court_id}

        # Real booking — requires CSRF token.
        csrf_token = await self._get_csrf_token()
        if not csrf_token:
            raise RuntimeError(
                "Could not obtain CSRF token. The booking page must be "
                "loaded at least once in the browser session."
            )

        headers = {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.app_base_url}/book/{self.club_slug}",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": self.app_base_url,
        }

        url = f"{self.app_base_url}/api/courts/{court_id}/booking_player"
        logger.info("Booking: POST {} ...", url)

        resp = await self._post_json(url, json_body=payload, headers=headers)
        return resp

    async def _get_csrf_token(self, page_path: Optional[str] = None) -> Optional[str]:
        """
        Fetch the CSRF token from a PBP page's <meta> tag.

        Rails embeds it as:
            <meta name="csrf-token" content="k-x-Pget0Iv...">

        Tries the provided page_path first, then falls back to the
        booking page and home page.
        """
        urls_to_try = []
        if page_path:
            urls_to_try.append(f"{self.app_base_url}{page_path}")
        urls_to_try.extend([
            f"{self.app_base_url}/book/{self.club_slug}",
            f"{self.app_base_url}/home",
        ])

        for url in urls_to_try:
            try:
                resp = await self._get_raw(url)
                if not resp:
                    continue
                text = resp if isinstance(resp, str) else resp.decode("utf-8", errors="replace")
                m = re.search(
                    r'<meta\s+name="csrf-token"\s+content="([^"]+)"', text
                )
                if not m:
                    m = re.search(
                        r'<meta\s+content="([^"]+)"\s+name="csrf-token"', text
                    )
                if m:
                    token = m.group(1)
                    logger.debug("CSRF token from {}: {}...", url[:60], token[:30])
                    return token
            except Exception as exc:
                logger.debug("CSRF fetch failed for {}: {}", url[:60], exc)
        return None

    async def book_program(
        self,
        clinic_id: int,
        plan_id: int,
        clinic_lesson_ids: list[int],
        program_slug: str = "",
        payment_method: str = "card",
        card_details: Optional[dict] = None,
        notes: str = "",
        dry_run: bool = True,
    ) -> dict:
        """
        Book a program/event/clinic session.

        HAR-verified endpoint (2026-05-14 Pickle Haus booking):

            POST /api/public/clinics/{clinic_id}
            {
              "plan_id": 251575,
              "user_child_id": null,
              "clinic_lesson_ids": [3234882],
              "free_passes": [],
              "payment": {
                "method": "card",
                "card_details": {id, last4, brand, exp_month, exp_year, is_default},
                "coupon": {"code": ""},
                "payment_intent_id": ""
              },
              "notes": ""
            }

        Args:
            clinic_id: The program/clinic ID (from catalog or React props).
            plan_id: The pricing plan ID (from prices/packages in React props).
            clinic_lesson_ids: List of session IDs to book (from sessions[].id).
            program_slug: Program URL slug (for CSRF token + Referer).
            payment_method: "card" or potentially "accounts_receivable".
            card_details: Saved card dict {id, last4, brand, exp_month, exp_year}.
                         If None, will auto-fetch the default saved card.
            notes: Optional booking notes.
            dry_run: If True (default), shows payload without booking.
        """
        # Auto-fetch card if not provided.
        if payment_method == "card" and not card_details:
            cards = await self.saved_cards()
            if not cards:
                raise ValueError(
                    "No saved cards found. Add a card in PBP first, "
                    "or specify card_details manually."
                )
            # Pick the default card, or first available.
            card_details = next(
                (c for c in cards if c.get("is_default")), cards[0]
            )

        payload = {
            "plan_id": plan_id,
            "user_child_id": None,
            "clinic_lesson_ids": clinic_lesson_ids,
            "free_passes": [],
            "payment": {
                "method": payment_method,
                "card_details": {
                    "id": card_details.get("id"),
                    "last4": card_details.get("last4"),
                    "brand": card_details.get("brand"),
                    "exp_month": card_details.get("exp_month"),
                    "exp_year": card_details.get("exp_year"),
                    "is_default": card_details.get("is_default", True),
                } if card_details else {},
                "coupon": {"code": ""},
                "payment_intent_id": "",
            },
            "notes": notes,
        }

        if dry_run:
            logger.info(
                "DRY RUN — would POST /api/public/clinics/{} with:\n{}",
                clinic_id,
                json.dumps(payload, indent=2, default=str),
            )
            return {"dry_run": True, "payload": payload, "clinic_id": clinic_id}

        # Real booking — requires CSRF token.
        csrf_token = await self._get_csrf_token(
            page_path=f"/programs/{program_slug}" if program_slug else None,
        )

        headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": self.app_base_url,
            "Referer": (
                f"{self.app_base_url}/programs/{program_slug}"
                if program_slug
                else f"{self.app_base_url}/home"
            ),
        }
        if csrf_token:
            headers["X-CSRF-Token"] = csrf_token

        url = f"{self.app_base_url}/api/public/clinics/{clinic_id}"
        logger.info("Booking program: POST {} ...", url)

        resp = await self._post_json(url, json_body=payload, headers=headers)
        return resp

    # ----- programs ----------------------------------------------------

    async def whoami(self) -> Optional[int]:
        """
        Discover the authenticated user's PBP numeric ID without knowing
        it upfront.

        Strategy: call GET /api/users/me or similar. If that doesn't
        exist, returns None and the caller falls back to a price probe.
        """
        for path in ["/api/users/me", "/api/v1/users/me", "/api/profile"]:
            try:
                data = await self._get_json(path)
                if isinstance(data, dict):
                    uid = data.get("id") or data.get("user_id")
                    if uid:
                        return int(uid)
            except Exception:
                continue
        return None

    async def programs(self, facility_id: int) -> list[dict]:
        """Public catalog endpoint. Also reachable anonymously."""
        data = await self._get_json(
            "/api/public/clinics",
            params={"search": "", "facility_id": facility_id},
        )
        return (data or {}).get("clinics") or []

    async def program_detail_html(self, slug: str) -> Optional[str]:
        """
        Fetch a program's detail page as HTML. The session data is
        embedded as data-react-props on a div with class
        `ClinicStepperIndividualSesions`. Caller is responsible for
        parsing the HTML out — `_extract_react_props_from_html` does it.

        Returns the HTML string or None on failure.
        """
        path = f"/programs/{slug}"
        # Quick raw GET — we want HTML, not JSON, so we bypass _get_json.
        now = asyncio.get_event_loop().time()
        wait = self._rate_limit - (now - self._last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = asyncio.get_event_loop().time()
        try:
            resp = await self._client.get(path)
            if resp.status_code != 200:
                return None
            return resp.text
        except Exception as exc:
            logger.debug("Program detail fetch failed for {}: {}", slug, exc)
            return None


def _extract_react_props_from_html(
    html: str, react_class: str = "ClinicStepperIndividualSesions",
) -> Optional[dict]:
    """
    Pull the JSON payload out of `<div data-react-class="X" data-react-props="{...}">`
    in PBP's server-rendered HTML.

    The attribute is HTML-entity-encoded JSON (e.g. `&quot;` for `"`).
    We extract with a regex, html-decode, then JSON.parse.

    Falls back to scanning for ANY data-react-class containing "Clinic"
    or "Session" if the exact class name isn't found — different venues
    may use slightly different component names.

    Returns the parsed dict, or None if the component isn't on the page.
    """
    if not html:
        return None

    # Try the exact class name first, then fallbacks.
    classes_to_try = [
        react_class,
        "ClinicStepperIndividualSessions",  # alternate spelling (double s)
        "ClinicStepper",                    # Melbourne Pickle Club + others
    ]

    for cls in classes_to_try:
        patterns = [
            rf'data-react-class=["\']{re.escape(cls)}["\'][^>]*?'
            rf'data-react-props=["\']([^"\']+)["\']',
            rf'data-react-props=["\']([^"\']+)["\'][^>]*?'
            rf'data-react-class=["\']{re.escape(cls)}["\']',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                try:
                    import html as _html_module
                    decoded = _html_module.unescape(m.group(1))
                    result = json.loads(decoded)
                    if cls != react_class:
                        logger.debug(
                            "React props found under class '{}' "
                            "(not '{}')", cls, react_class,
                        )
                    return result
                except Exception as exc:
                    logger.debug("React-props parse failed for {}: {}", cls, exc)

    # Last resort: find ANY data-react-class that looks session/clinic related.
    all_classes = re.findall(
        r'data-react-class=["\']([^"\']*(?:Clinic|Session|Stepper)[^"\']*)["\']',
        html, re.IGNORECASE,
    )
    for cls in all_classes:
        if cls in [c for c in classes_to_try]:
            continue
        logger.debug("Trying discovered React class: {}", cls)
        patterns = [
            rf'data-react-class=["\']{re.escape(cls)}["\'][^>]*?'
            rf'data-react-props=["\']([^"\']+)["\']',
            rf'data-react-props=["\']([^"\']+)["\'][^>]*?'
            rf'data-react-class=["\']{re.escape(cls)}["\']',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                try:
                    import html as _html_module
                    decoded = _html_module.unescape(m.group(1))
                    result = json.loads(decoded)
                    logger.info(
                        "React props found under NEW class '{}' — "
                        "please report this so it can be added as a "
                        "primary pattern.", cls,
                    )
                    return result
                except Exception:
                    pass

    if all_classes:
        logger.debug(
            "Found data-react-class elements but none matched: {}",
            all_classes,
        )
    return None


def _local_midnight_unix(day: date) -> int:
    """
    Unix timestamp for `day` at midnight in the venue's local timezone.

    PBP expects timestamps for midnight on the QUERIED day in the
    facility's TZ. The venue's TZ is `DEFAULT_TIMEZONE` (Melbourne).
    We compute it without pytz by using zoneinfo (stdlib in 3.9+).
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    except Exception:
        # Fallback: treat as UTC. Most clubs will work even with this
        # because PBP's day boundaries are loose, but Melbourne users
        # would get one-day-off bugs around midnight.
        return int(datetime.combine(
            day, datetime.min.time()
        ).timestamp())
    dt = datetime.combine(day, datetime.min.time()).replace(tzinfo=tz)
    return int(dt.timestamp())


# ---------------------------------------------------------------------------
# Orchestrator: fast-path scrape using the API client
# ---------------------------------------------------------------------------


async def run_fast(
    *,
    cookies: dict[str, str],
    user_id: Optional[int],
    facility_id: int,
    club_slug: str,
    club_name: str,
    start_date: date,
    number_of_days: int,
    skip_courts: bool,
    skip_programs: bool,
    enrich_pricing: bool = True,
    app_base_url: str = APP_BASE_URL,
    cookie_cache_path: Optional[Path] = None,
) -> ScrapeResult:
    """
    Pure-HTTP scrape using the cookies we already have.

    Phases (run in parallel where possible):
      1. Programs catalog → expand into per-date stubs.
      2. Per-day available_hours → 30-min slot records.
      3. Per available 60-min slot (or longer), per-court availability +
         pricing fetched in parallel.

    Pricing enrichment is optional (off saves ~100 API calls for 7 days
    × 16 evening slots × 4 courts), but on by default since it's the
    whole point of going fast — answers the "what does it cost" question
    in one batch.
    """
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    end_date = start_date + timedelta(days=number_of_days - 1)

    notes: list[str] = []
    sessions: list[SessionRecord] = []
    booking_rules: Optional[BookingRules] = None

    async with PlayByPointAPI(
        cookies=cookies, app_base_url=app_base_url, club_slug=club_slug,
    ) as api:

        # Resolve user_id early — needed for accurate court pricing.
        # The price endpoint returns affiliation=null and price=0 unless
        # players=<user_id>. We try three sources in order:
        #   1. Already provided (from cache or --user-id flag)
        #   2. API whoami endpoints
        #   3. Price probe (extract from prices_per_user[0].id)
        if not user_id:
            user_id = await api.whoami()
            if user_id:
                logger.info("Discovered user_id={} via whoami.", user_id)
            else:
                logger.debug(
                    "whoami failed — will probe via price call later."
                )

        # Phase 1: programs catalog + per-program detail enrichment.

        # Auto-detect the pickleball surface name for this venue.
        # Most venues use "pickleball" but some use variants like
        # "indoor_pickleball". We query court_types and pick the first
        # surface containing "pickle" (case-insensitive).
        surface = "pickleball"  # default
        if not skip_courts:
            try:
                ct = await api.court_types(facility_id)
                if ct:
                    pickle_surfaces = [
                        s for s in ct
                        if "pickle" in (s.get("surface") or "").lower()
                    ]
                    if pickle_surfaces:
                        surface = pickle_surfaces[0]["surface"]
                        if surface != "pickleball":
                            logger.info(
                                "Surface auto-detected: '{}' (from court_types)",
                                surface,
                            )
                    elif ct:
                        # No pickleball surface found — use the first one.
                        surface = ct[0]["surface"]
                        logger.warning(
                            "No pickleball surface found. Using '{}'. "
                            "Available surfaces: {}",
                            surface,
                            [s.get("surface") for s in ct],
                        )
            except Exception as exc:
                logger.debug("court_types lookup failed: {}", exc)

        program_records: list[SessionRecord] = []
        if not skip_programs:
            logger.info("Fast mode: fetching programs catalog …")
            catalog = await api.programs(facility_id)
            logger.info("  → {} programs.", len(catalog))
            program_records = _expand_catalog_to_records(
                catalog, start_date, number_of_days, app_base_url,
            )
            logger.info("  → {} per-date session stubs.",
                        len(program_records))

            # Enrich each ACTIVE program (one with at least one stub in
            # the window) by fetching its detail page and parsing the
            # embedded React props. All in parallel.
            active_slugs = sorted({
                s.raw.get("slug") for s in program_records
                if s.raw.get("slug")
            })
            if active_slugs:
                logger.info(
                    "  → enriching {} active programs (parallel detail "
                    "fetch + react-props parse) …",
                    len(active_slugs),
                )
                sem = asyncio.Semaphore(8)

                async def _fetch_one(slug):
                    async with sem:
                        html = await api.program_detail_html(slug)
                        if not html:
                            logger.debug("  detail fetch returned None for {}", slug)
                            return slug, None
                        props = _extract_react_props_from_html(html)
                        if not props:
                            logger.debug(
                                "  no react-props found for {} "
                                "(html length={})",
                                slug, len(html),
                            )
                        return slug, props

                results = await asyncio.gather(
                    *(_fetch_one(s) for s in active_slugs)
                )
                enriched_total = 0
                for slug, props in results:
                    if not props:
                        continue
                    enriched_total += _merge_program_detail_into_stubs(
                        program_records, slug, props,
                    )
                logger.info("  → enriched {} session(s).", enriched_total)

            sessions.extend(program_records)

        # Phase 2: court availability per day, in parallel.
        if not skip_courts:
            logger.info("Fast mode: fetching availability for {} days …",
                        number_of_days)
            days = [start_date + timedelta(days=i)
                    for i in range(number_of_days)]
            hours_results = await asyncio.gather(
                *(api.available_hours(facility_id, d, surface=surface) for d in days),
                return_exceptions=True,
            )

            # Capture booking_rules from the first successful response.
            for hr in hours_results:
                if isinstance(hr, Exception) or not hr:
                    continue
                rules = (hr.get("meta") or {}).get("specific_rules") or {}
                if rules and booking_rules is None:
                    step_seconds = rules.get("playerBookingTimeStep")
                    min_hours = rules.get(
                        "minimum_time_slots_allowed_to_book"
                    )
                    booking_rules = BookingRules(
                        slot_length_minutes=(
                            int(step_seconds / 60)
                            if isinstance(step_seconds, (int, float))
                            else None
                        ),
                        max_consecutive_hours=rules.get(
                            "max_consecutive_hours"
                        ),
                        min_advance_hours=rules.get(
                            "amount_of_hours_prior_to_book"
                        ),
                        max_advance_hours=rules.get(
                            "amount_of_max_hours_prior_to_book"
                        ),
                        next_open_at=rules.get("nextOpenScheduleDateTime"),
                        raw={**rules, "_min_hours_parsed": min_hours},
                    )
                    break

            # Build slot records and collect candidates for pricing.
            slot_length_min = (
                booking_rules.slot_length_minutes
                if booking_rules else 30
            )
            pricing_jobs = []  # list of (day, start_sec, end_sec)

            for day, hr in zip(days, hours_results):
                if isinstance(hr, Exception):
                    logger.warning("available_hours failed for {}: {}",
                                   day, hr)
                    notes.append(f"available_hours failed for {day}: {hr}")
                    continue
                slots = (hr or {}).get("available_hours") or []
                for slot in slots:
                    start_sec = slot.get("seconds_from_midnight")
                    if not isinstance(start_sec, (int, float)):
                        continue
                    start_sec = int(start_sec)
                    end_sec = start_sec + slot_length_min * 60
                    rec = SessionRecord(
                        date=day.isoformat(),
                        start_time=_seconds_to_hhmm(start_sec),
                        end_time=_seconds_to_hhmm(end_sec),
                        session_type="Court Slot",
                        title=slot.get("schedule"),
                        status=(
                            "Available" if slot.get("available")
                            else ("Waitlist" if slot.get("in_waitlist")
                                  else "Unavailable")
                        ),
                        pricing_tier=slot.get("shift"),
                        external_id=(
                            str(slot.get("facility_schedule_id"))
                            if slot.get("facility_schedule_id") else None
                        ),
                        source="xhr",
                        raw=slot,
                    )
                    sessions.append(rec)
                    # Queue a pricing call for AVAILABLE slots only.
                    # We price each slot at its slot-length (30 min).
                    if slot.get("available") and enrich_pricing:
                        pricing_jobs.append((day, start_sec, end_sec, rec))

            logger.info("  → {} slots collected, {} need pricing.",
                        sum(1 for s in sessions
                            if s.session_type == "Court Slot"),
                        len(pricing_jobs))

            # Phase 3: pricing + per-court availability
            if pricing_jobs:
                # --- Discover user_id dynamically ---
                logger.debug("Phase 3: user_id={} (type={}), {} pricing jobs",
                             user_id, type(user_id).__name__, len(pricing_jobs))
                # real pricing (with players=1 it returns affiliation=null
                # and price=0). BUT every price response contains the
                # authenticated user's id in prices_per_user[0].id,
                # even when the price is zero. So we make one probe call,
                # extract the user_id, then use it for all real calls.
                if not user_id:
                    try:
                        probe_day, probe_s, probe_e, _ = pricing_jobs[0]
                        probe_courts = await api.available_courts(
                            facility_id, probe_day, probe_s, probe_e,
                            surface=surface,
                        )
                        if probe_courts:
                            probe_cid = probe_courts[0]["id"]
                            probe_end = probe_s + 3600  # 1hr window
                            probe_resp = await api.court_price(
                                probe_cid, probe_day, probe_s, probe_end,
                                user_id=None,  # probe with players=1
                            )
                            ppu = (probe_resp.get("prices_per_user") or [{}])
                            if ppu and isinstance(ppu[0], dict):
                                user_id = ppu[0].get("id")
                                if user_id:
                                    api._user_id = user_id
                                    logger.info(
                                        "Discovered user_id={} from "
                                        "price probe.", user_id,
                                    )
                                    # Cache it so future runs skip the probe.
                                    try:
                                        if cookie_cache_path and cookie_cache_path.exists():
                                            cached_data = json.loads(
                                                cookie_cache_path.read_text(
                                                    encoding="utf-8"
                                                )
                                            )
                                            cached_data["user_id"] = user_id
                                            cookie_cache_path.write_text(
                                                json.dumps(cached_data),
                                                encoding="utf-8",
                                            )
                                    except Exception:
                                        pass
                    except Exception as exc:
                        logger.debug("user_id probe failed: {}", exc)

                sem = asyncio.Semaphore(10)

                async def _enrich(day, s_sec, e_sec, slot_rec):
                    async with sem:
                        try:
                            min_minutes = (
                                booking_rules.min_booking_minutes
                                if booking_rules and
                                booking_rules.min_booking_minutes
                                else 60
                            )
                            price_end_sec = s_sec + min_minutes * 60

                            # Step 1: get available courts for this slot.
                            courts = await api.available_courts(
                                facility_id, day, s_sec, e_sec,
                                surface=surface,
                            )

                            if not isinstance(courts, list) or not courts:
                                return slot_rec, courts, None

                            # Step 2: price the FIRST available court.
                            # Using a court that's actually free at this
                            # slot is critical — PBP returns all-zeros
                            # when you price an unavailable court.
                            first_court_id = courts[0].get("id")
                            if not first_court_id:
                                return slot_rec, courts, None

                            logger.debug(
                                "Pricing court {} for {} {} to {}",
                                first_court_id,
                                slot_rec.date,
                                slot_rec.start_time,
                                _seconds_to_hhmm(s_sec + min_minutes * 60),
                            )
                            price = await api.court_price(
                                first_court_id, day, s_sec, price_end_sec,
                                user_id=user_id,
                            )
                            return slot_rec, courts, price
                        except Exception as exc:
                            logger.debug("Enrich failed {}: {}", slot_rec.date, exc)
                            return slot_rec, None, None

                results = await asyncio.gather(
                    *(_enrich(d, s, e, r)
                      for d, s, e, r in pricing_jobs),
                )

                priced = 0
                first_zero_logged = False
                for slot_rec, courts, price in results:
                    if isinstance(courts, list):
                        names = [c.get("name") for c in courts
                                 if c.get("name")]
                        slot_rec.raw["available_courts"] = courts
                        if names:
                            slot_rec.court_name = " / ".join(names)
                            slot_rec.court_number = ", ".join(
                                str(c.get("id")) for c in courts
                            )
                    elif isinstance(courts, Exception):
                        logger.debug("available_courts error: {}", courts)
                    if isinstance(price, dict):
                        price_str = _extract_price_string(price)
                        if price_str:
                            slot_rec.price = price_str
                            priced += 1
                        else:
                            if not first_zero_logged:
                                logger.debug(
                                    "First zero-price full payload:\n{}",
                                    json.dumps(price, indent=2)[:2000],
                                )
                                first_zero_logged = True
                        slot_rec.raw["pricing"] = price
                    elif isinstance(price, Exception):
                        logger.debug("court_price error: {}", price)
                logger.info("  → priced {} / {} available slots.",
                            priced, len(pricing_jobs))

                # Static tier-price fallback: for any Available court slot
                # that still has no price (API returned zeros or wasn't called),
                # apply known per-tier rates. Rates are per-hour for one court,
                # non-member pricing. Only primetime is confirmed from XHR;
                # others are estimates marked with "~".
                #
                # For The Jar (facility 597): primetime = $55/hr confirmed.
                # Update TIER_PRICE_PER_HOUR below when other rates are known.
                # Load confirmed tier prices from cache (written by probe-prices).
                # Falls back to hardcoded estimates for uncached tiers.
                _confirmed_rates = _load_tier_prices()
                TIER_PRICE_PER_HOUR: dict[str, tuple[float, bool]] = {
                    # tier name: ($/hr, is_confirmed)
                    "primetime":     (_confirmed_rates.get("primetime", 55.0),
                                     "primetime" in _confirmed_rates),
                    "day":           (_confirmed_rates.get("day", 40.0),
                                     "day" in _confirmed_rates),
                    "lowtime":       (_confirmed_rates.get("lowtime", 27.50),
                                     "lowtime" in _confirmed_rates),
                    "night":         (_confirmed_rates.get("night", 45.0),
                                     "night" in _confirmed_rates),
                    "Discount Time": (_confirmed_rates.get("Discount Time",
                                      _confirmed_rates.get("discount_time", 27.50)),
                                     ("Discount Time" in _confirmed_rates
                                      or "discount_time" in _confirmed_rates)),
                    "discount_time": (_confirmed_rates.get("discount_time",
                                      _confirmed_rates.get("Discount Time", 27.50)),
                                     ("discount_time" in _confirmed_rates
                                      or "Discount Time" in _confirmed_rates)),
                    "discount":      (_confirmed_rates.get("discount", 27.50),
                                     "discount" in _confirmed_rates),
                }
                min_booking_hrs = (
                    booking_rules.min_booking_minutes / 60
                    if booking_rules and booking_rules.min_booking_minutes
                    else 1.0
                )
                tier_priced = 0
                for slot_rec in sessions:
                    if (slot_rec.session_type == "Court Slot"
                            and slot_rec.status == "Available"
                            and not slot_rec.price
                            and slot_rec.pricing_tier):
                        tier = slot_rec.pricing_tier
                        # Try exact match first, then lowercase
                        entry = TIER_PRICE_PER_HOUR.get(tier) or \
                                TIER_PRICE_PER_HOUR.get(tier.lower())
                        if entry:
                            rate, confirmed = entry
                            total = rate * min_booking_hrs
                            prefix = "" if confirmed else "~"
                            slot_rec.price = (
                                f"{prefix}${total:.2f}/hr"
                                if min_booking_hrs == 1.0
                                else f"{prefix}${total:.2f}"
                            )
                            tier_priced += 1
                if tier_priced:
                    logger.info(
                        "  → applied static tier pricing to {} slots "
                        "(~ = estimated; primetime confirmed from XHR).",
                        tier_priced,
                    )

        scrape_finished_at = datetime.now(
            timezone.utc).isoformat().replace("+00:00", "Z")

    if booking_rules and booking_rules.min_booking_minutes:
        logger.info(
            "Booking rules: slot={}min, min={}min, max={}h, "
            "advance window={}h",
            booking_rules.slot_length_minutes,
            booking_rules.min_booking_minutes,
            booking_rules.max_consecutive_hours,
            booking_rules.max_advance_hours,
        )

    return ScrapeResult(
        club_name=club_name,
        club_slug=club_slug,
        base_url=app_base_url,
        scrape_started_at=started_at,
        scrape_finished_at=scrape_finished_at,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        sessions=sessions,
        raw_responses=[],   # not collected in fast mode
        notes=notes,
        booking_rules=booking_rules,
    )


def _extract_price_string(price_payload: dict) -> Optional[str]:
    """
    Pull the headline price out of /api/courts/<id>/price payload.

    Shape (per real XHRs):
      total.total_to_pay                   : actual cost for the booked duration
      total.original_reservation_fare      : sticker price
      prices_per_user[0].price.shift_prices[0].price : hourly rate
      prices_per_user[0].price.reservation_fare      : per-user cost

    We prefer `total.total_to_pay` when non-zero (real booking), else
    fall back to `prices_per_user[0].price.reservation_fare`, else the
    `shift_prices[0].price * picked` (hourly × fraction-of-hour).
    """
    total = price_payload.get("total") or {}
    headline = total.get("total_to_pay")
    if isinstance(headline, (int, float)) and headline > 0:
        return f"${headline:.2f}"

    # Headline was 0 — happens for some queries. Try per-user reservation_fare.
    ppu = price_payload.get("prices_per_user") or []
    if ppu and isinstance(ppu[0], dict):
        price_block = ppu[0].get("price") or {}
        fare = price_block.get("reservation_fare")
        if isinstance(fare, (int, float)) and fare > 0:
            return f"${fare:.2f}"

        # Last resort: derive from shift_prices (hourly × picked-hours).
        shift_prices = price_block.get("shift_prices") or []
        if shift_prices and isinstance(shift_prices[0], dict):
            sp = shift_prices[0]
            try:
                hourly = float(sp.get("price"))
                picked = float(sp.get("picked", 1.0))
                derived = hourly * picked
                if derived > 0:
                    return f"${derived:.2f}"
            except (TypeError, ValueError):
                pass
    return None


def _merge_program_detail_into_stubs(
    sessions: list[SessionRecord],
    slug: str,
    payload: dict,
) -> int:
    """
    Standalone version of PlayByPointScraper._merge_enrichments — merges
    PBP's program detail payload into existing catalog stubs by
    (slug, lesson_date).

    Handles BOTH component formats:
      - ClinicStepperIndividualSesions: payload.sessions[] with
        individual_prices[], player_count, capacity, teacher_names
      - ClinicStepper: payload may have sessions[] or packages[] or
        prices at the top level; sessions may use 'price' instead of
        'individual_prices'

    Returns the number of stubs enriched.
    """
    if not isinstance(payload, dict):
        return 0
    detail_sessions = payload.get("sessions") or []

    # Log the payload structure once for debugging new venues.
    if detail_sessions:
        sample = detail_sessions[0] if isinstance(detail_sessions[0], dict) else {}
        logger.debug(
            "Program detail keys for '{}': top={}, session[0]={}",
            slug,
            sorted(k for k in payload if k != "sessions"),
            sorted(sample.keys()) if sample else "empty",
        )
    else:
        logger.debug(
            "Program detail for '{}': no sessions[] found. "
            "Top-level keys: {}",
            slug, sorted(payload.keys()),
        )
        return 0

    user_affiliation = (payload.get("currentUserAffiliation")
                        or payload.get("current_user_affiliation")
                        or "non_member")

    # Extract top-level / package-level prices as fallback.
    # ClinicStepper sometimes puts prices in packages[] or at root.
    fallback_price_str: Optional[str] = None
    packages = payload.get("packages") or payload.get("clinic_packages") or []
    if packages and isinstance(packages, list):
        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            # Try member-matching first, then any price.
            pkg_price = pkg.get("price") or pkg.get("amount")
            pkg_cat = pkg.get("player_category") or pkg.get("affiliation") or ""
            if isinstance(pkg_price, (int, float)) and pkg_price > 0:
                if user_affiliation in str(pkg_cat).lower() or not fallback_price_str:
                    fallback_price_str = f"${float(pkg_price):.2f}"

    # Also check for a single top-level price field.
    for price_key in ("price", "amount", "cost", "fee"):
        v = payload.get(price_key)
        if isinstance(v, (int, float)) and v > 0 and not fallback_price_str:
            fallback_price_str = f"${float(v):.2f}"

    matched = 0

    for ds in detail_sessions:
        if not isinstance(ds, dict):
            continue
        iso_date = ds.get("lesson_date")
        if not iso_date:
            continue

        hour_start = ds.get("hour_start")
        hour_end = ds.get("hour_end")
        start_time = (_seconds_to_hhmm(hour_start)
                      if isinstance(hour_start, (int, float)) else None)
        end_time = (_seconds_to_hhmm(hour_end)
                    if isinstance(hour_end, (int, float)) else None)

        # Price extraction — try multiple strategies.
        price_str: Optional[str] = None

        # Strategy 1: individual_prices[] with player_category matching.
        # (ClinicStepperIndividualSesions format)
        for p in (ds.get("individual_prices") or []):
            if (isinstance(p, dict)
                    and p.get("player_category") == user_affiliation
                    and "price" in p):
                price_str = f"${float(p['price']):.2f}"
                break
        if price_str is None:
            for p in (ds.get("individual_prices") or []):
                if (isinstance(p, dict)
                        and p.get("player_category") == "non_member"
                        and "price" in p):
                    price_str = f"${float(p['price']):.2f}"
                    break

        # Strategy 2: direct price field on the session.
        # (ClinicStepper format — some venues put price directly on session)
        if price_str is None:
            for price_key in ("price", "amount", "cost", "fee",
                              "session_price", "per_session_price"):
                v = ds.get(price_key)
                if isinstance(v, (int, float)) and v > 0:
                    price_str = f"${float(v):.2f}"
                    break
                elif isinstance(v, str) and v.strip():
                    try:
                        fv = float(v.replace("$", "").replace(",", ""))
                        if fv > 0:
                            price_str = f"${fv:.2f}"
                            break
                    except ValueError:
                        pass

        # Strategy 3: prices[] array on the session (another variant).
        if price_str is None:
            for p in (ds.get("prices") or []):
                if isinstance(p, dict):
                    v = p.get("price") or p.get("amount")
                    if isinstance(v, (int, float)) and v > 0:
                        price_str = f"${float(v):.2f}"
                        break

        # Strategy 4: fall back to package/top-level price.
        if price_str is None:
            price_str = fallback_price_str

        player_count = ds.get("player_count")
        capacity = ds.get("capacity")
        spots_available = None
        if isinstance(player_count, int) and isinstance(capacity, int):
            spots_available = max(capacity - player_count, 0)

        if (isinstance(spots_available, int)
                and isinstance(capacity, int) and capacity > 0):
            status = "Full" if spots_available == 0 else "Available"
        else:
            status = ds.get("status") or None

        teachers = ds.get("teacher_names") or []
        teacher_str = ", ".join(t for t in teachers if t) or None

        # Find matching stub.
        for stub in sessions:
            if (stub.raw.get("slug") == slug
                    and stub.date == iso_date):
                if start_time:
                    stub.start_time = start_time
                if end_time:
                    stub.end_time = end_time
                if price_str:
                    stub.price = price_str
                if spots_available is not None:
                    stub.spots_available = spots_available
                if isinstance(capacity, int):
                    stub.max_spots = capacity
                if status:
                    stub.status = status
                if teacher_str and teacher_str not in (stub.title or ""):
                    base = stub.title or ""
                    stub.title = f"{base} · {teacher_str}".strip(" ·")
                stub.external_id = (str(ds.get("id"))
                                    or stub.external_id)
                stub.raw["detail"] = ds
                matched += 1
                break
    return matched


def _expand_catalog_to_records(
    catalog: list[dict],
    start_date: date,
    number_of_days: int,
    app_base_url: str,
) -> list[SessionRecord]:
    """
    Standalone version of PlayByPointScraper._expand_catalog so the
    fast path doesn't need a Scraper instance.
    """
    PBP_WEEKDAY_TO_PY = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    end_date = start_date + timedelta(days=number_of_days - 1)
    out: list[SessionRecord] = []
    for prog in catalog:
        try:
            ps = datetime.strptime(prog.get("start_date", ""),
                                   "%Y-%m-%d").date()
            pe = datetime.strptime(prog.get("end_date", ""),
                                   "%Y-%m-%d").date()
        except ValueError:
            continue
        pbp_days = prog.get("future_week_days") or []
        py_days = {PBP_WEEKDAY_TO_PY.get(d) for d in pbp_days}
        py_days.discard(None)
        if not py_days:
            continue
        window_start = max(start_date, ps)
        window_end = min(end_date, pe)
        if window_start > window_end:
            continue
        slug = (prog.get("url") or "").rsplit("/", 1)[-1]
        d = window_start
        while d <= window_end:
            if d.weekday() in py_days:
                out.append(SessionRecord(
                    date=d.isoformat(),
                    title=prog.get("name"),
                    session_type=_normalise_category(prog.get("category")),
                    skill_level=prog.get("ntrp_str") or None,
                    max_spots=prog.get("capacity"),
                    booking_link=(
                        f"{app_base_url}{prog.get('url')}"
                        if prog.get("url") else None
                    ),
                    external_id=(str(prog.get("id"))
                                 if prog.get("id") else None),
                    source="xhr",
                    raw={"catalog": prog, "slug": slug},
                ))
            d += timedelta(days=1)
    return out


def _looks_like_session_cookie(name: str) -> bool:
    """
    Identify the PBP session cookie. Devise apps typically use a name
    like `_<app>_session` (e.g. `_playbypoint_session`), but PBP's
    naming has varied. Match conservatively.
    """
    n = name.lower()
    return (
        "session" in n
        or n in {"_pbp", "pbp_session", "auth_token", "remember_user_token"}
    )


async def harvest_cookies_via_playwright(
    email: str,
    password: str,
    headless: bool = True,
    proxy: Optional[str] = None,
) -> tuple[dict[str, str], Optional[int]]:
    """
    Run a minimal Playwright session to log in and harvest cookies + user_id.

    Returns (cookies_dict, user_id). user_id may be None if PBP doesn't
    expose it via /home; in that case the caller should pass it via CLI
    or env var (PBP_USER_ID).

    This is the only Playwright touchpoint in fast mode. After this,
    everything goes through PlayByPointAPI.
    """
    # Construct a temporary scraper just to reuse its login machinery.
    scraper = PlayByPointScraper(
        start_date=date.today(),
        number_of_days=1,
        headless=headless,
        proxy=proxy,
        email=email,
        password=password,
        skip_courts=True,
        skip_programs=True,
    )

    from playwright.async_api import async_playwright
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    cookies: dict[str, str] = {}
    user_id: Optional[int] = None

    async with async_playwright() as pw:
        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            "channel": "chrome",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        try:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                viewport={"width": 1366, "height": 900},
                locale="en-AU",
                timezone_id=DEFAULT_TIMEZONE,
                user_agent=PBP_USER_AGENT,
                **launch_kwargs,
            )
        except Exception:
            launch_kwargs.pop("channel", None)
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                viewport={"width": 1366, "height": 900},
                locale="en-AU",
                timezone_id=DEFAULT_TIMEZONE,
                user_agent=PBP_USER_AGENT,
                **launch_kwargs,
            )

        page = await context.new_page()
        if _HAS_STEALTH and _stealth_apply is not None:
            try:
                await _stealth_apply(page)
            except Exception:
                pass

        # Reuse the scraper's login & response handlers.
        scraper._raw_responses = []  # noqa: SLF001

        async def _on_response(response):
            url = response.url
            # Extract user_id from any API URL that contains it, e.g.:
            # /api/users/1973346/current_facility
            # /api/users/preferred_facilities?user_id=1973346
            nonlocal user_id
            if user_id is None:
                m = re.search(r"/api/users/(\d+)/", url)
                if m:
                    user_id = int(m.group(1))
                    logger.debug("Harvested user_id={} from {}", user_id, url[:80])
                else:
                    m2 = re.search(r"[?&]user_id=(\d+)", url)
                    if m2:
                        user_id = int(m2.group(1))
                        logger.debug("Harvested user_id={} from {}", user_id, url[:80])

        page.on("response", _on_response)

        await scraper._login(page)  # noqa: SLF001

        # Visit /home to trigger the current_facility XHR (which leaks
        # the user_id in its URL).
        try:
            await page.goto(f"{APP_BASE_URL}/home",
                            wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)
        except Exception:
            pass

        # Harvest cookies. We loop over ALL cookies and keep anything
        # remotely PBP-related — PBP sets cookies on multiple subdomains
        # (.playbypoint.com, app.playbypoint.com) and our filter was
        # previously too strict.
        raw_cookies = await context.cookies()
        logger.debug("Raw cookies from context ({}):", len(raw_cookies))
        for c in raw_cookies:
            domain = c.get("domain", "")
            name = c.get("name", "")
            logger.debug("  domain={!r} name={!r}", domain, name)
            if "playbypoint" in domain.lower():
                cookies[name] = c["value"]

        await context.close()

    return cookies, user_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

cli = typer.Typer(add_completion=False, help=__doc__)


async def probe_tier_prices(
    cookies: dict[str, str],
    club_slug: str,
    facility_id: int,
    app_base_url: str = APP_BASE_URL,
    headless: bool = True,
    proxy: Optional[str] = None,
    cache_path: Optional[Path] = None,
) -> dict[str, float]:
    """
    Use Playwright to navigate the booking page, click one available slot
    per pricing tier, and capture the real price from the API response.

    This mimics exactly what the browser does — the price endpoint only
    returns real data when called in the context of the booking UI.

    Returns a dict of {tier_name: price_per_hour} for confirmed tiers.
    Also writes the result to cache_path (default: tier_prices.json) so
    subsequent fast-mode runs can use confirmed prices without re-probing.
    """
    from playwright.async_api import async_playwright

    cache_path = cache_path or Path("tier_prices.json")
    confirmed: dict[str, float] = {}

    logger.info("Probing tier prices via Playwright browser session…")

    async with async_playwright() as pw:
        launch_kwargs: dict[str, Any] = {"headless": headless}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}

        browser = await pw.chromium.launch(**launch_kwargs)
        ctx = await browser.new_context(
            extra_http_headers={
                "Accept-Language": "en-AU,en;q=0.9",
            }
        )

        # Inject harvested cookies so we're already logged in.
        await ctx.add_cookies([
            {
                "name": k, "value": v,
                "domain": "app.playbypoint.com",
                "path": "/",
            }
            for k, v in cookies.items()
        ])

        page = await ctx.new_page()

        # Intercept price API responses.
        tier_prices_seen: dict[str, float] = {}

        async def _on_response(response):
            url = response.url
            if "/api/courts/" not in url or "/price" not in url:
                return
            try:
                data = await response.json()
                ppu = (data.get("prices_per_user") or [{}])
                if not ppu:
                    return
                price_obj = ppu[0].get("price", {})
                shift_prices = price_obj.get("shift_prices") or []
                total = price_obj.get("total_to_pay", 0)
                hours = price_obj.get("total_hours", 1) or 1
                if total and float(total) > 0 and shift_prices:
                    shift = shift_prices[0].get("shift", "unknown")
                    rate = float(total) / float(hours)
                    if shift not in tier_prices_seen:
                        tier_prices_seen[shift] = rate
                        logger.info(
                            "  Confirmed: {} = ${:.2f}/hr", shift, rate
                        )
            except Exception as exc:
                logger.debug("Price intercept error: {}", exc)

        page.on("response", _on_response)

        # Navigate to booking page.
        booking_url = f"{app_base_url}/book/{club_slug}"
        logger.info("  Navigating to {}…", booking_url)
        await page.goto(booking_url, wait_until="networkidle", timeout=30_000)

        # Get available slots from the API directly — same as fast mode.
        async with PlayByPointAPI(
            cookies=cookies, app_base_url=app_base_url, club_slug=club_slug,
        ) as api:
            today = date.today()
            slots_by_tier: dict[str, tuple] = {}  # tier -> (day, s, e, court_id)

            # Scan 14 days to find at least one slot per tier.
            for offset in range(14):
                day = today + timedelta(days=offset)
                try:
                    raw = await api.available_hours(facility_id, day)
                except Exception:
                    continue
                # available_hours returns either a list or a dict with
                # an 'available_hours' key depending on the API version.
                if isinstance(raw, dict):
                    hours = raw.get("available_hours") or []
                else:
                    hours = raw or []
                available = [h for h in hours
                             if isinstance(h, dict) and h.get("available")]
                for slot in available:
                    shift = slot.get("shift", "")
                    if not shift or shift in slots_by_tier:
                        continue
                    s_sec = int(slot.get("seconds_from_midnight", 0))
                    e_sec = s_sec + 1800
                    try:
                        courts = await api.available_courts(
                            facility_id, day, s_sec, e_sec
                        )
                    except Exception:
                        continue
                    if courts:
                        slots_by_tier[shift] = (day, s_sec, e_sec, courts[0]["id"])
                        logger.debug(
                            "  Found {} slot: {} {}",
                            shift, day, _seconds_to_hhmm(s_sec),
                        )

                if len(slots_by_tier) >= 6:
                    break  # have enough tiers

            logger.info(
                "  Found slots for {} tier(s): {}",
                len(slots_by_tier), list(slots_by_tier.keys()),
            )

            # For each tier, navigate to the booking page with that slot
            # selected so the price XHR fires.
            import time as _time
            for shift, (day, s_sec, e_sec, court_id) in slots_by_tier.items():
                if shift in tier_prices_seen:
                    continue
                try:
                    # Build the booking URL with slot params as query string.
                    # PBP's booking page reads these to pre-select the slot.
                    ts = int(_time.time())
                    price_url = (
                        f"{app_base_url}/api/courts/{court_id}/price"
                        f"?date={ts}"
                        f"&admin_book=false"
                        f"&hour_start={s_sec}"
                        f"&hour_end={s_sec + 3600}"
                        f"&players=1"
                        f"&reservation_type=1"
                        f"&payment_method=card"
                    )
                    # Fetch via page.evaluate so it uses the browser's session
                    # cookies and context — exactly as if the user clicked.
                    result = await page.evaluate(f"""
                        async () => {{
                            const r = await fetch("{price_url}", {{
                                credentials: "include",
                                headers: {{
                                    "Accept": "application/json",
                                    "Referer": "{booking_url}",
                                    "X-Requested-With": "XMLHttpRequest",
                                }}
                            }});
                            return await r.json();
                        }}
                    """)
                    ppu = (result.get("prices_per_user") or [{}])
                    if ppu:
                        price_obj = ppu[0].get("price", {})
                        total = float(price_obj.get("total_to_pay", 0) or 0)
                        hours = float(price_obj.get("total_hours", 1) or 1)
                        shift_prices = price_obj.get("shift_prices") or []
                        if total > 0 and shift_prices:
                            rate = total / hours
                            tier_prices_seen[shift] = rate
                            logger.info(
                                "  Confirmed via fetch: {} = ${:.2f}/hr",
                                shift, rate,
                            )
                        else:
                            logger.debug(
                                "  {} returned 0 (affiliation={})",
                                shift,
                                (ppu[0].get("price") or {}).get("affiliation"),
                            )
                except Exception as exc:
                    logger.debug("  Probe failed for {}: {}", shift, exc)

        await browser.close()

    confirmed = dict(tier_prices_seen)

    if confirmed:
        # Merge with any existing cache (don't overwrite already-confirmed tiers).
        existing: dict[str, float] = {}
        if cache_path.exists():
            try:
                existing = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        merged = {**existing, **confirmed}
        cache_path.write_text(
            json.dumps(merged, indent=2), encoding="utf-8"
        )
        logger.info(
            "Saved {} confirmed tier price(s) to {}",
            len(confirmed), cache_path,
        )
    else:
        logger.warning(
            "No tier prices confirmed. The price API may require an active "
            "booking session initiated from the UI. Try --no-headless to "
            "watch the browser and click a slot manually."
        )

    return confirmed


def _load_tier_prices(cache_path: Optional[Path] = None) -> dict[str, float]:
    """Load confirmed tier prices from cache, returns {} if not found."""
    path = cache_path or Path("tier_prices.json")
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _configure_logging(verbose: bool) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="<green>{time:HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | {message}",
    )


def _load_cached_session() -> tuple[dict[str, str], Optional[int], Optional[str]]:
    """
    Load cookies + user_id from the cache, invalidating if the .env
    email has changed since the cache was written.

    Returns (cookies, user_id, cached_email).
    """
    load_dotenv()
    cookie_cache = USER_DATA_DIR / "cookies.json"
    cookies: dict[str, str] = {}
    user_id: Optional[int] = None
    cached_email: Optional[str] = None

    current_email = os.getenv("PBP_EMAIL")

    if cookie_cache.exists():
        try:
            cached = json.loads(cookie_cache.read_text(encoding="utf-8"))
            cached_email = cached.get("email")

            if (current_email and cached_email
                    and current_email.lower() != cached_email.lower()):
                logger.info(
                    "Email changed ({} → {}). Invalidating cookie cache.",
                    cached_email, current_email,
                )
                cookie_cache.unlink(missing_ok=True)
                return {}, None, None

            cookies = cached.get("cookies") or {}
            user_id = cached.get("user_id")
            cached_email = cached.get("email")
            cached_at = cached.get("cached_at")
            if cookies:
                logger.info("Using cached cookies (cached at {}).", cached_at)
        except Exception:
            pass

    return cookies, user_id, cached_email


@cli.command(name="search")
def search_cmd(
    query: str = typer.Argument(..., help="Search query (e.g. 'melbourne', 'pickleball palace')"),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", "-v"),
) -> None:
    """
    Search for venues on PlayByPoint.

    Examples:
        python extract_thejar.py search melbourne
        python extract_thejar.py search "pickle club"
        python extract_thejar.py search ravenhall
    """
    _configure_logging(verbose)

    cookies, _, _ = _load_cached_session()

    if not cookies:
        logger.warning(
            "No cached cookies found. Run the main scraper once first "
            "to log in and cache cookies."
        )
        # Try anyway — might work without auth
        cookies = {}

    async def _search():
        async with PlayByPointAPI(cookies=cookies) as api:
            results = await api.search_facilities(query)
            return results

    results = asyncio.run(_search())

    if not results:
        console = Console()
        console.print(f"[yellow]No venues found for '{query}'.[/yellow]")
        return

    console = Console()
    table = Table(
        title=f"PlayByPoint venues matching '{query}'",
        show_lines=False,
    )
    table.add_column("ID", style="bold")
    table.add_column("Name")
    table.add_column("City")
    table.add_column("Courts")
    table.add_column("Surface")
    table.add_column("Slug")
    table.add_column("Online Booking")

    for f in results:
        # Extract slug from book_url: "/book/nplpickleball" → "nplpickleball"
        book_url = f.get("book_url") or ""
        slug = book_url.rsplit("/", 1)[-1] if book_url else ""
        table.add_row(
            str(f.get("id", "")),
            f.get("name", ""),
            f.get("city", ""),
            str(f.get("court_number", "")),
            f.get("surface_list", ""),
            slug,
            "✓" if f.get("allow_online_booking") else "✗",
        )

    console.print(table)
    console.print(
        f"\n[dim]To scrape a venue, use its slug:[/dim]\n"
        f"  python extract_thejar.py --slug <SLUG> "
        f"--facility-id <ID> --days 7 --mode fast"
    )


@cli.command(name="find")
def find_cmd(
    venues: str = typer.Argument(
        ...,
        help="Comma-separated venue specs: slug:facility_id,slug:id,... "
             "Example: nplpickleball:597,MelbournePickleClub:1383",
    ),
    date: str = typer.Option(
        ..., "--date",
        help="Date to search (YYYY-MM-DD)",
    ),
    start_time: str = typer.Option(
        "16:00", "--from",
        help="Start of window (HH:MM, 24h format)",
    ),
    end_time: str = typer.Option(
        "22:00", "--to",
        help="End of window (HH:MM, 24h format)",
    ),
    pricing: bool = typer.Option(True, "--pricing/--no-pricing"),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", "-v"),
) -> None:
    """
    Find available courts across multiple venues in a time window.

    Examples:
        python extract_thejar.py find nplpickleball:597,MelbournePickleClub:1383,easternindoorpickleballclub:1009 --date 2026-05-15 --from 16:00 --to 22:00
        python extract_thejar.py find nplpickleball:597 --date 2026-05-15 --from 18:00 --to 20:00
    """
    _configure_logging(verbose)

    # Parse venues.
    venue_list: list[tuple[str, int]] = []
    for v in venues.split(","):
        v = v.strip()
        if ":" not in v:
            logger.error("Invalid venue spec '{}' — use slug:facility_id", v)
            raise typer.Exit(1)
        slug, fid_str = v.rsplit(":", 1)
        venue_list.append((slug.strip(), int(fid_str.strip())))

    # Parse date and times.
    try:
        target_date = datetime.strptime(date.strip(), "%Y-%m-%d").date()
    except ValueError:
        logger.error("Invalid date '{}' — use YYYY-MM-DD", date)
        raise typer.Exit(1)

    def _parse_hhmm(s: str) -> int:
        """Parse HH:MM to seconds from midnight."""
        h, m = s.strip().split(":")
        return int(h) * 3600 + int(m) * 60

    from_sec = _parse_hhmm(start_time)
    to_sec = _parse_hhmm(end_time)

    # Load cookies.
    cookies, user_id, _ = _load_cached_session()
    if not cookies:
        logger.error("No cached cookies. Run a normal scrape first to log in.")
        raise typer.Exit(1)

    async def _find():
        all_results: list[dict] = []

        for slug, fid in venue_list:
            logger.info("Checking {} (facility {}) …", slug, fid)

            async with PlayByPointAPI(
                cookies=cookies, club_slug=slug,
            ) as api:
                if user_id:
                    api._user_id = user_id

                # Auto-detect surface.
                surface = "pickleball"
                try:
                    ct = await api.court_types(fid)
                    pickle_surfaces = [
                        s for s in (ct or [])
                        if "pickle" in (s.get("surface") or "").lower()
                    ]
                    if pickle_surfaces:
                        surface = pickle_surfaces[0]["surface"]
                except Exception:
                    pass

                # Get availability for the target date.
                try:
                    hours_data = await api.available_hours(
                        fid, target_date, surface=surface,
                    )
                except Exception as exc:
                    logger.warning("  {} failed: {}", slug, exc)
                    continue

                if isinstance(hours_data, dict):
                    slots = hours_data.get("available_hours") or []
                else:
                    slots = hours_data or []

                # Filter to the requested time window.
                window_slots = []
                for slot in slots:
                    if not isinstance(slot, dict):
                        continue
                    sec = slot.get("seconds_from_midnight")
                    if not isinstance(sec, (int, float)):
                        continue
                    if from_sec <= sec < to_sec and slot.get("available"):
                        window_slots.append(slot)

                if not window_slots:
                    logger.info("  → no available slots in window.")
                    continue

                logger.info(
                    "  → {} available slot(s) in window.", len(window_slots),
                )

                # For each available slot, get courts + price.
                for slot in window_slots:
                    sec = int(slot["seconds_from_midnight"])
                    slot_end = sec + 1800  # 30min slot
                    start_hhmm = f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}"
                    end_hhmm = (
                        f"{slot_end // 3600:02d}:"
                        f"{(slot_end % 3600) // 60:02d}"
                    )
                    shift = slot.get("shift", "")

                    # Get specific courts.
                    court_names: list[str] = []
                    court_id = None
                    try:
                        courts = await api.available_courts(
                            fid, target_date, sec, slot_end,
                            surface=surface,
                        )
                        if isinstance(courts, list):
                            court_names = [
                                c.get("name", f"Court {c.get('id')}")
                                for c in courts
                            ]
                            if courts:
                                court_id = courts[0].get("id")
                    except Exception:
                        pass

                    # Get price.
                    price_str = ""
                    if pricing and court_id and user_id:
                        try:
                            min_end = sec + 3600  # price for 1hr
                            price_data = await api.court_price(
                                court_id, target_date, sec, min_end,
                                user_id=user_id,
                            )
                            price_str = _extract_price_string(price_data) or ""
                        except Exception:
                            pass

                    all_results.append({
                        "venue": slug,
                        "facility_id": fid,
                        "date": target_date.isoformat(),
                        "start": start_hhmm,
                        "end": end_hhmm,
                        "tier": shift,
                        "courts": " / ".join(court_names),
                        "price_per_hr": price_str,
                    })

        return all_results

    results = asyncio.run(_find())

    console = Console()
    if not results:
        console.print(
            f"[yellow]No available courts found between {start_time} "
            f"and {end_time} on {date} at any venue.[/yellow]"
        )
        return

    table = Table(
        title=(
            f"Available courts · {target_date.strftime('%A %d %B %Y')} · "
            f"{start_time}–{end_time}"
        ),
        show_lines=False,
    )
    table.add_column("Venue", style="bold")
    table.add_column("Time")
    table.add_column("Tier")
    table.add_column("Courts Available")
    table.add_column("Price/hr")

    for r in sorted(results, key=lambda x: (x["venue"], x["start"])):
        table.add_row(
            r["venue"],
            f"{r['start']}–{r['end']}",
            r["tier"],
            r["courts"],
            r["price_per_hr"],
        )

    console.print(table)
    console.print(
        f"\n[dim]{len(results)} available slot(s) across "
        f"{len(venue_list)} venue(s).[/dim]"
    )


@cli.command(name="book")
def book_cmd(
    venue: str = typer.Argument(
        ..., help="Venue spec: slug:facility_id (e.g. nplpickleball:597)",
    ),
    date: str = typer.Option(
        ..., "--date", help="Date to book (YYYY-MM-DD)",
    ),
    time: str = typer.Option(
        ..., "--time", help="Start time (HH:MM, 24h format, e.g. 14:00)",
    ),
    duration: int = typer.Option(
        60, "--duration", help="Duration in minutes (default: 60)",
    ),
    court: Optional[int] = typer.Option(
        None, "--court-id",
        help="Specific court ID to book. If omitted, books the first available.",
    ),
    payment: str = typer.Option(
        "card", "--payment",
        help="Payment method: card, accounts_receivable, prepaid",
    ),
    confirm: bool = typer.Option(
        False, "--confirm",
        help="Actually execute the booking. Without this flag, "
             "shows a dry-run preview only.",
    ),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", "-v"),
) -> None:
    """
    Book a court at a PlayByPoint venue.

    By default, runs in DRY-RUN mode — shows what would be booked
    without actually doing it. Add --confirm to execute for real.

    Examples (dry run):
        python extract_thejar.py book nplpickleball:597 --date 2026-05-15 --time 14:00

    Examples (real booking):
        python extract_thejar.py book nplpickleball:597 --date 2026-05-15 --time 14:00 --confirm

    With specific court and payment:
        python extract_thejar.py book nplpickleball:597 --date 2026-05-15 --time 14:00 --duration 90 --court-id 6220 --payment accounts_receivable --confirm
    """
    _configure_logging(verbose)
    console = Console()

    # Parse venue.
    if ":" not in venue:
        logger.error("Invalid venue spec '{}' — use slug:facility_id", venue)
        raise typer.Exit(1)
    slug, fid_str = venue.rsplit(":", 1)
    facility_id = int(fid_str.strip())

    # Parse date/time.
    try:
        target_date = datetime.strptime(date.strip(), "%Y-%m-%d").date()
    except ValueError:
        logger.error("Invalid date '{}' — use YYYY-MM-DD", date)
        raise typer.Exit(1)

    h, m = time.strip().split(":")
    start_sec = int(h) * 3600 + int(m) * 60
    end_sec = start_sec + duration * 60
    start_hhmm = f"{int(h):02d}:{int(m):02d}"
    end_h, end_m = divmod(end_sec, 3600)
    end_m = (end_sec % 3600) // 60
    end_hhmm = f"{end_h:02d}:{end_m:02d}"

    # Load cookies + user_id.
    cookies, user_id, _ = _load_cached_session()
    if not cookies:
        logger.error("No cached cookies. Run a normal scrape first to log in.")
        raise typer.Exit(1)
    if not user_id:
        logger.error("No user_id cached. Run a scrape with --mode fast first.")
        raise typer.Exit(1)

    async def _book():
        async with PlayByPointAPI(
            cookies=cookies, club_slug=slug,
        ) as api:
            api._user_id = user_id

            # Step 1: Auto-detect surface.
            surface = "pickleball"
            try:
                ct = await api.court_types(facility_id)
                pickle_surfaces = [
                    s for s in (ct or [])
                    if "pickle" in (s.get("surface") or "").lower()
                ]
                if pickle_surfaces:
                    surface = pickle_surfaces[0]["surface"]
            except Exception:
                pass

            # Step 2: Check availability for that slot.
            console.print(
                f"\n[bold]Checking availability…[/bold]\n"
                f"  Venue:    {slug} (facility {facility_id})\n"
                f"  Date:     {target_date.isoformat()}\n"
                f"  Time:     {start_hhmm} – {end_hhmm} ({duration} min)\n"
                f"  Surface:  {surface}\n"
            )

            hours_data = await api.available_hours(
                facility_id, target_date, surface=surface,
            )
            if isinstance(hours_data, dict):
                all_slots = hours_data.get("available_hours") or []
            else:
                all_slots = []

            # Check each 30-min slot in our window is available.
            needed_slots = []
            for sec in range(start_sec, end_sec, 1800):
                slot = next(
                    (s for s in all_slots
                     if isinstance(s, dict)
                     and int(s.get("seconds_from_midnight", -1)) == sec),
                    None,
                )
                if not slot:
                    console.print(
                        f"  [red]✗[/red] No slot data for "
                        f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}"
                    )
                    return
                if not slot.get("available"):
                    t = f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}"
                    console.print(
                        f"  [red]✗ Slot {t} is not available.[/red] "
                        f"Cannot book this window."
                    )
                    return
                needed_slots.append(slot)

            console.print(
                f"  [green]✓ All {len(needed_slots)} slots available[/green]"
            )

            # Step 3: Find available courts.
            courts_data = await api.available_courts(
                facility_id, target_date, start_sec, end_sec,
                surface=surface,
            )
            if not courts_data:
                console.print("  [red]✗ No courts available for this window.[/red]")
                return

            court_id_to_book = court
            court_name = None
            if court_id_to_book:
                # Verify the requested court is in the available list.
                match = next(
                    (c for c in courts_data if c.get("id") == court_id_to_book),
                    None,
                )
                if not match:
                    console.print(
                        f"  [red]✗ Court {court_id_to_book} is not available.[/red]\n"
                        f"  Available courts: "
                        f"{', '.join(c.get('name', str(c.get('id'))) for c in courts_data)}"
                    )
                    return
                court_name = match.get("name", str(court_id_to_book))
            else:
                # Pick the first available court.
                court_id_to_book = courts_data[0].get("id")
                court_name = courts_data[0].get("name", str(court_id_to_book))

            all_court_names = [
                c.get("name", str(c.get("id"))) for c in courts_data
            ]
            console.print(
                f"  [green]✓ {len(courts_data)} court(s) available:[/green] "
                f"{', '.join(all_court_names)}"
            )
            console.print(
                f"  → Booking: [bold]{court_name}[/bold] (ID {court_id_to_book})"
            )

            # Step 4: Get price.
            price_str = "unknown"
            try:
                price_data = await api.court_price(
                    court_id_to_book, target_date, start_sec, end_sec,
                    user_id=user_id,
                )
                price_str = _extract_price_string(price_data) or "unknown"
            except Exception as exc:
                logger.debug("Price lookup failed: {}", exc)

            console.print(f"  💰 Price: [bold]{price_str}[/bold]")

            # Step 5: Summary + confirm/dry-run.
            console.print("")
            console.print("[bold]═══ Booking Summary ═══[/bold]")
            console.print(f"  📍 Venue:    {slug}")
            console.print(f"  📅 Date:     {target_date.isoformat()}")
            console.print(
                f"  🕐 Time:     {start_hhmm} – {end_hhmm} ({duration} min)"
            )
            console.print(f"  🏓 Court:    {court_name} (ID {court_id_to_book})")
            console.print(f"  💰 Price:    {price_str}")
            console.print(f"  💳 Payment:  {payment}")
            console.print(f"  👤 User:     {user_id}")
            console.print("")

            if not confirm:
                console.print(
                    "[yellow]DRY RUN — no booking made.[/yellow]\n"
                    "Add [bold]--confirm[/bold] to execute this booking.\n"
                )

                # Show the exact command to run.
                cmd = (
                    f"python extract_thejar.py book {venue} "
                    f"--date {date} --time {time} --duration {duration} "
                    f"--court-id {court_id_to_book} --payment {payment} "
                    f"--confirm"
                )
                console.print(f"[dim]$ {cmd}[/dim]\n")

                # Also do a dry-run through the API to show the payload.
                result = await api.book_court(
                    court_id=court_id_to_book,
                    day=target_date,
                    start_seconds=start_sec,
                    end_seconds=end_sec,
                    user_id=user_id,
                    payment_method=payment,
                    dry_run=True,
                )
                console.print("[dim]Payload that would be sent:[/dim]")
                console.print(
                    f"[dim]{json.dumps(result.get('payload', {}), indent=2, default=str)}[/dim]"
                )
                return

            # ═══ REAL BOOKING ═══
            console.print("[bold red]⚠  EXECUTING REAL BOOKING…[/bold red]")

            try:
                result = await api.book_court(
                    court_id=court_id_to_book,
                    day=target_date,
                    start_seconds=start_sec,
                    end_seconds=end_sec,
                    user_id=user_id,
                    payment_method=payment,
                    dry_run=False,
                )

                # Check response.
                if isinstance(result, dict):
                    errors = result.get("errors") or result.get("error")
                    if errors:
                        console.print(
                            f"\n[red]✗ Booking failed:[/red] {errors}"
                        )
                        return

                    res_id = (
                        result.get("reservation", {}).get("id")
                        or result.get("id")
                        or result.get("reservation_id")
                    )
                    console.print(
                        f"\n[bold green]✓ BOOKED![/bold green]"
                    )
                    if res_id:
                        console.print(
                            f"  Reservation ID: {res_id}"
                        )
                    console.print(
                        f"  {court_name} · {target_date} · "
                        f"{start_hhmm}–{end_hhmm} · {price_str}"
                    )
                else:
                    console.print(
                        f"\n[yellow]Response:[/yellow] {result}"
                    )

            except Exception as exc:
                console.print(
                    f"\n[red]✗ Booking failed with error:[/red] {exc}"
                )

    asyncio.run(_book())


@cli.command(name="book-program")
def book_program_cmd(
    venue: str = typer.Argument(
        ..., help="Venue spec: slug:facility_id (e.g. picklehaus:1485)",
    ),
    clinic_id: int = typer.Option(
        ..., "--clinic-id", help="Program/clinic ID (from catalog)",
    ),
    session_id: int = typer.Option(
        ..., "--session-id", help="Session ID to book (clinic_lesson_id)",
    ),
    plan_id: int = typer.Option(
        ..., "--plan-id", help="Pricing plan ID (from packages/prices)",
    ),
    program_slug: str = typer.Option(
        "", "--program-slug", help="Program URL slug (for CSRF/referer)",
    ),
    payment: str = typer.Option(
        "card", "--payment", help="Payment method: card, accounts_receivable",
    ),
    confirm: bool = typer.Option(
        False, "--confirm",
        help="Actually execute the booking. Without this, dry-run only.",
    ),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", "-v"),
) -> None:
    """
    Book a program/event/clinic session at a PlayByPoint venue.

    By default runs in DRY-RUN mode. Add --confirm to execute.

    Example (dry run):
        python extract_thejar.py book-program picklehaus:1485 --clinic-id 160796 --session-id 3234882 --plan-id 251575 --program-slug wednesday---intermediate-advance-social

    Example (real booking):
        python extract_thejar.py book-program picklehaus:1485 --clinic-id 160796 --session-id 3234882 --plan-id 251575 --program-slug wednesday---intermediate-advance-social --confirm
    """
    _configure_logging(verbose)
    console = Console()

    # Parse venue.
    if ":" not in venue:
        logger.error("Invalid venue spec '{}' — use slug:facility_id", venue)
        raise typer.Exit(1)
    slug, fid_str = venue.rsplit(":", 1)

    # Load cookies.
    cookies, user_id, _ = _load_cached_session()
    if not cookies:
        logger.error("No cached cookies. Run a normal scrape first to log in.")
        raise typer.Exit(1)

    async def _book():
        async with PlayByPointAPI(
            cookies=cookies, club_slug=slug,
        ) as api:
            if user_id:
                api._user_id = user_id

            # Fetch saved card.
            cards = await api.saved_cards()
            if not cards:
                console.print("[red]✗ No saved cards found on this account.[/red]")
                return
            card = next((c for c in cards if c.get("is_default")), cards[0])

            # Summary.
            console.print("")
            console.print("[bold]═══ Program Booking ═══[/bold]")
            console.print(f"  📍 Venue:      {slug}")
            console.print(f"  🏓 Clinic ID:  {clinic_id}")
            console.print(f"  📅 Session ID: {session_id}")
            console.print(f"  💰 Plan ID:    {plan_id}")
            console.print(f"  💳 Card:       {card.get('brand','?')} ····{card.get('last4','?')}")
            console.print(f"  👤 User:       {user_id}")
            console.print(f"  💳 Payment:    {payment}")
            console.print("")

            if not confirm:
                result = await api.book_program(
                    clinic_id=clinic_id,
                    plan_id=plan_id,
                    clinic_lesson_ids=[session_id],
                    program_slug=program_slug,
                    payment_method=payment,
                    card_details=card,
                    dry_run=True,
                )
                console.print("[yellow]DRY RUN — no booking made.[/yellow]")
                console.print("Add [bold]--confirm[/bold] to execute.\n")
                cmd = (
                    f"python extract_thejar.py book-program {venue} "
                    f"--clinic-id {clinic_id} --session-id {session_id} "
                    f"--plan-id {plan_id} "
                    f"--program-slug {program_slug} "
                    f"--payment {payment} --confirm"
                )
                console.print(f"[dim]$ {cmd}[/dim]\n")
                return

            # ═══ REAL BOOKING ═══
            console.print("[bold red]⚠  EXECUTING REAL BOOKING…[/bold red]")

            try:
                result = await api.book_program(
                    clinic_id=clinic_id,
                    plan_id=plan_id,
                    clinic_lesson_ids=[session_id],
                    program_slug=program_slug,
                    payment_method=payment,
                    card_details=card,
                    dry_run=False,
                )

                if isinstance(result, dict):
                    errors = result.get("errors") or result.get("error")
                    if errors:
                        console.print(f"\n[red]✗ Booking failed:[/red] {errors}")
                        return

                    console.print("\n[bold green]✓ BOOKED![/bold green]")
                    console.print(
                        f"  Clinic {clinic_id} · Session {session_id} · "
                        f"Plan {plan_id}"
                    )
                    # Print any useful response fields.
                    for key in ["id", "reservation_id", "message", "status"]:
                        if key in result:
                            console.print(f"  {key}: {result[key]}")
                else:
                    console.print(f"\n[yellow]Response:[/yellow] {result}")

            except Exception as exc:
                console.print(f"\n[red]✗ Booking failed:[/red] {exc}")

    asyncio.run(_book())


@cli.command(name="probe-prices")
def probe_prices_cmd(
    headless: bool = typer.Option(True, "--headless/--no-headless"),
    proxy: Optional[str] = typer.Option(None, "--proxy"),
    slug: str = typer.Option(DEFAULT_CLUB_SLUG, "--slug"),
    facility_id: int = typer.Option(DEFAULT_FACILITY_ID, "--facility-id"),
    app_base: str = typer.Option(APP_BASE_URL, "--app-base"),
    out: Optional[str] = typer.Option(None, "--out", help="Output JSON path (default: tier_prices.json)"),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", "-v"),
) -> None:
    """
    Probe real court hire prices for every tier by running a browser
    session that fetches prices in the same context the booking UI uses.

    Results are saved to tier_prices.json (or --out path) and used
    automatically by subsequent scraper runs.

    Run once whenever pricing tiers change:
        python extract_thejar.py probe-prices --no-headless
    """
    _configure_logging(verbose)

    email = os.getenv("PBP_EMAIL")
    password = os.getenv("PBP_PASSWORD")
    cache_path = Path(out) if out else Path("tier_prices.json")

    # Load cached cookies.
    cookie_cache = USER_DATA_DIR / "cookies.json"
    cookies: dict[str, str] = {}
    user_id: Optional[int] = None
    if cookie_cache.exists():
        try:
            d = json.loads(cookie_cache.read_text(encoding="utf-8"))
            cookies = d.get("cookies") or {}
            user_id = d.get("user_id")
            logger.info("Using cached cookies.")
        except Exception:
            pass

    if not cookies and email and password:
        logger.info("No cached cookies — logging in via Playwright…")
        cookies, user_id = asyncio.run(
            harvest_cookies_via_playwright(email, password, headless, proxy)
        )

    if not cookies:
        logger.error("No cookies available. Set PBP_EMAIL / PBP_PASSWORD or run fast mode first.")
        raise typer.Exit(1)

    confirmed = asyncio.run(probe_tier_prices(
        cookies=cookies,
        club_slug=slug,
        facility_id=facility_id,
        app_base_url=app_base,
        headless=headless,
        proxy=proxy,
        cache_path=cache_path,
    ))

    if confirmed:
        typer.echo(f"\nConfirmed prices written to {cache_path}:")
        for tier, rate in sorted(confirmed.items()):
            typer.echo(f"  {tier:20s} ${rate:.2f}/hr")
    else:
        typer.echo("\nNo prices confirmed — see debug output above.")
        raise typer.Exit(1)


@cli.command()
def main(
    date_: str = typer.Option(
        date.today().isoformat(),
        "--date", "-d",
        help="Start date YYYY-MM-DD. Defaults to today.",
    ),
    days: int = typer.Option(
        7, "--days", "-n",
        help="Number of days to scrape (1–60).",
    ),
    slug: str = typer.Option(
        DEFAULT_CLUB_SLUG, "--slug",
        help="Club URL slug — used in app.playbypoint.com/book/<slug>.",
    ),
    facility_id: int = typer.Option(
        DEFAULT_FACILITY_ID, "--facility-id",
        help="Numeric PBP facility ID — used to filter "
             "/programs?facility_id=<id>.",
    ),
    app_base: str = typer.Option(
        APP_BASE_URL, "--app-base",
        help="Override the authenticated app base URL (default: "
             "https://app.playbypoint.com).",
    ),
    club_name: str = typer.Option(
        DEFAULT_CLUB_NAME, "--club-name",
        help="Display name for the club (used in output).",
    ),
    headless: bool = typer.Option(
        True, "--headless/--no-headless",
        help="Run Chromium headless or visible.",
    ),
    proxy: Optional[str] = typer.Option(
        None, "--proxy", help="Proxy URL, e.g. http://user:pass@host:port",
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o",
        help="Output JSON path. Defaults to thejar_schedule_<date>.json",
    ),
    skip_courts: bool = typer.Option(False, "--skip-courts"),
    skip_programs: bool = typer.Option(False, "--skip-programs"),
    dump_xhr: bool = typer.Option(
        False, "--dump-xhr",
        help="Save every captured XHR JSON payload to debug/xhr_*.json "
             "for inspection.",
    ),
    mode: str = typer.Option(
        "fast", "--mode", "-m",
        help=(
            "Execution mode:\n"
            "  • fast      — pure HTTP against PBP's internal APIs "
            "(~5s for 7 days). Recommended.\n"
            "  • reliable  — full Playwright scrape (~60-90s). "
            "Use if API mode breaks.\n"
            "  • hybrid    — try fast, fall back to reliable on auth "
            "failure or empty result."
        ),
    ),
    pricing: bool = typer.Option(
        True, "--pricing/--no-pricing",
        help="In fast mode, fetch per-slot pricing + per-court "
             "availability. ~100 extra API calls for a 7-day window.",
    ),
    user_id: Optional[int] = typer.Option(
        None, "--user-id",
        help="PBP numeric user ID (auto-discovered after login).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """
    Extract The Jar's schedule from PlayByPoint and print + save it.
    """
    _configure_logging(verbose)
    load_dotenv()

    try:
        start_date = datetime.strptime(date_, "%Y-%m-%d").date()
    except ValueError:
        logger.error("Invalid --date: {} (expected YYYY-MM-DD)", date_)
        raise typer.Exit(code=2)

    if not 1 <= days <= 60:
        logger.error("--days must be 1..60 (got {})", days)
        raise typer.Exit(code=2)

    mode = mode.lower()
    if mode not in {"fast", "reliable", "hybrid"}:
        logger.error("--mode must be fast | reliable | hybrid (got {})",
                     mode)
        raise typer.Exit(code=2)

    out_path = out or Path(f"thejar_schedule_{start_date.isoformat()}.json")
    email = os.getenv("PBP_EMAIL")
    password = os.getenv("PBP_PASSWORD")
    if user_id is None:
        env_uid = os.getenv("PBP_USER_ID")
        if env_uid and env_uid.isdigit():
            user_id = int(env_uid)
            logger.debug("user_id={} from PBP_USER_ID env var", user_id)

    if mode == "reliable":
        result = _run_reliable(
            app_base=app_base, slug=slug, facility_id=facility_id,
            club_name=club_name, start_date=start_date, days=days,
            headless=headless, proxy=proxy, email=email,
            password=password, skip_courts=skip_courts,
            skip_programs=skip_programs, dump_xhr=dump_xhr,
        )
    elif mode == "fast":
        result = _run_fast_with_login(
            email=email, password=password,
            headless=headless, proxy=proxy,
            app_base=app_base, slug=slug, facility_id=facility_id,
            club_name=club_name, start_date=start_date, days=days,
            skip_courts=skip_courts, skip_programs=skip_programs,
            enrich_pricing=pricing, user_id=user_id,
        )
    else:  # hybrid
        try:
            result = _run_fast_with_login(
                email=email, password=password,
                headless=headless, proxy=proxy,
                app_base=app_base, slug=slug, facility_id=facility_id,
                club_name=club_name, start_date=start_date, days=days,
                skip_courts=skip_courts, skip_programs=skip_programs,
                enrich_pricing=pricing, user_id=user_id,
            )
            # If fast returned nothing, fall back automatically.
            if not result.sessions:
                logger.warning(
                    "Fast mode returned no sessions — falling back to "
                    "reliable mode."
                )
                result = _run_reliable(
                    app_base=app_base, slug=slug, facility_id=facility_id,
                    club_name=club_name, start_date=start_date, days=days,
                    headless=headless, proxy=proxy, email=email,
                    password=password, skip_courts=skip_courts,
                    skip_programs=skip_programs, dump_xhr=dump_xhr,
                )
        except (PermissionError, Exception) as exc:
            logger.warning("Fast mode failed ({}). Falling back to "
                           "reliable mode.", exc)
            result = _run_reliable(
                app_base=app_base, slug=slug, facility_id=facility_id,
                club_name=club_name, start_date=start_date, days=days,
                headless=headless, proxy=proxy, email=email,
                password=password, skip_courts=skip_courts,
                skip_programs=skip_programs, dump_xhr=dump_xhr,
            )

    write_json(result, out_path)
    print_table(result)


def _run_reliable(**kw) -> ScrapeResult:
    """Run the original Playwright scraper."""
    scraper = PlayByPointScraper(
        app_base_url=kw["app_base"],
        club_slug=kw["slug"],
        facility_id=kw["facility_id"],
        club_name=kw["club_name"],
        start_date=kw["start_date"],
        number_of_days=kw["days"],
        headless=kw["headless"],
        proxy=kw["proxy"],
        email=kw["email"],
        password=kw["password"],
        skip_courts=kw["skip_courts"],
        skip_programs=kw["skip_programs"],
        dump_xhr=kw["dump_xhr"],
    )
    return asyncio.run(scraper.scrape())


def _run_fast_with_login(**kw) -> ScrapeResult:
    """
    Login (Playwright, one-shot) + fast HTTP scrape.

    Caches the harvested cookies in USER_DATA_DIR/cookies.json so
    subsequent fast runs within the cookie lifetime skip the browser
    entirely.
    """
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    cookie_cache = USER_DATA_DIR / "cookies.json"

    cookies: dict[str, str] = {}
    user_id = kw.get("user_id")

    # Try cached cookies first.
    if cookie_cache.exists():
        try:
            cached = json.loads(cookie_cache.read_text(encoding="utf-8"))
            cached_email = cached.get("email")
            current_email = kw.get("email")

            # Invalidate cache if the .env email changed.
            if (current_email and cached_email
                    and current_email.lower() != cached_email.lower()):
                logger.info(
                    "Email changed ({} → {}). Invalidating cookie cache.",
                    cached_email, current_email,
                )
                cookies = {}
            else:
                cookies = cached.get("cookies") or {}
                if user_id is None:
                    user_id = cached.get("user_id")
                cached_at = cached.get("cached_at")
                logger.info("Using cached cookies (cached at {}).", cached_at)
        except Exception as exc:
            logger.debug("Cookie cache unreadable: {}", exc)
            cookies = {}

    def _has_session(c: dict) -> bool:
        return any(_looks_like_session_cookie(k) for k in c.keys())

    # If no usable cookies, do a Playwright login.
    if not _has_session(cookies):
        if not (kw.get("email") and kw.get("password")):
            raise RuntimeError(
                "Fast mode needs cached cookies or PBP_EMAIL/PBP_PASSWORD. "
                "Set creds in .env or run --mode=reliable once."
            )
        logger.info("Fast mode: logging in via Playwright to harvest "
                    "cookies …")
        cookies, harvested_uid = asyncio.run(
            harvest_cookies_via_playwright(
                email=kw["email"], password=kw["password"],
                headless=kw["headless"], proxy=kw["proxy"],
            )
        )
        # Log what we got so this never silently fails again.
        if cookies:
            logger.debug("Harvested cookies: {}",
                         sorted(cookies.keys()))
        if not _has_session(cookies):
            raise RuntimeError(
                f"Login completed but no session cookie was harvested. "
                f"Got cookies: {sorted(cookies.keys())}. "
                "Re-run with --mode=reliable to debug, or open an issue "
                "with the cookie names above."
            )
        if user_id is None:
            user_id = harvested_uid
        try:
            cookie_cache.write_text(json.dumps({
                "cookies": cookies,
                "user_id": user_id,
                "email": kw.get("email", ""),
                "cached_at": datetime.now(
                    timezone.utc).isoformat().replace("+00:00", "Z"),
            }), encoding="utf-8")
            logger.debug("Cached cookies to {}", cookie_cache)
        except Exception as exc:
            logger.debug("Could not write cookie cache: {}", exc)

    # user_id isn't strictly required (most endpoints work without it),
    # but the discovery endpoints need it. Fall back to 0 — the
    # downstream calls that need user_id will fail gracefully.
    return asyncio.run(run_fast(
        cookies=cookies,
        user_id=user_id,
        facility_id=kw["facility_id"],
        club_slug=kw["slug"],
        club_name=kw["club_name"],
        start_date=kw["start_date"],
        number_of_days=kw["days"],
        skip_courts=kw["skip_courts"],
        skip_programs=kw["skip_programs"],
        enrich_pricing=kw["enrich_pricing"],
        app_base_url=kw["app_base"],
        cookie_cache_path=cookie_cache,
    ))


if __name__ == "__main__":
    # If no subcommand given (first non-option arg isn't 'main' or
    # 'probe-prices'), default to 'main' so the user can just do:
    #   python extract_thejar.py --days 7 --mode fast
    # instead of:
    #   python extract_thejar.py main --days 7 --mode fast
    _known_cmds = {"main", "probe-prices", "search", "find", "book", "book-program"}
    # Find the first non-option argument — that's the subcommand.
    _first_arg = None
    for a in sys.argv[1:]:
        if not a.startswith("-"):
            _first_arg = a
            break
    if _first_arg not in _known_cmds:
        # Only inject 'main' if there are actual arguments beyond --help
        if "--help" not in sys.argv and "-h" not in sys.argv:
            sys.argv.insert(1, "main")
    cli()