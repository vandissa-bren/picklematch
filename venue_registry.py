"""
venue_registry.py -- the single answer to "what venues do we support?"

Reads the committed venues.json snapshot. No network, no Supabase call, no
runtime dependency: venue configuration is needed to build a booking request,
so making booking depend on a database read introduces a failure mode
(Supabase unavailable -> nobody can book) for data that changes a few times
a year. Supabase owns the registry; this file consumes the generated
snapshot. See generate_venue_snapshot.py.

The lookup layer, not the caller, enforces the distinctions:

    resolve(1664)   -> Venue(status='active', booking_enabled=True)
    resolve(1826)   -> Venue(status='delisted')   historical rows still work
    resolve('pickleplay') -> Unresolved('pickleplay')
    resolve(99999)  -> Unresolved('99999')

`Unresolved` is a RESULT, never a registry row. Historical data contains
values we cannot identify, and the fix is to return an explicit unresolved
state -- not to invent configuration so old rows fit.

Callers must never fall back to another venue. Two such fallbacks exist in
the frontend today (SessionsPage `?? VENUES[0]`, which silently renders an
unknown venue as The Jar) and one in the booking server (`courts[0]`). Every
function here either returns a Venue for the id asked for, or says it
cannot.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional, Union

SNAPSHOT_PATH = os.environ.get(
    "VENUE_SNAPSHOT_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "venues.json"),
)

VALID_STATUS = {"active", "delisted"}
VALID_FETCHER = {"court_blocks", "sportswell", None}
VALID_PLATFORM = {"playbypoint"}


@dataclass(frozen=True)
class Venue:
    facility_id: int
    platform: str
    slug: str
    name: str
    status: str
    fetcher: Optional[str]
    booking_enabled: bool

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_bookable(self) -> bool:
        """Active AND explicitly booking-enabled. Both, always."""
        return self.status == "active" and self.booking_enabled


@dataclass(frozen=True)
class Unresolved:
    """
    A venue reference we cannot identify. Carries the original value so the
    UI can say which reference failed rather than rendering a wrong venue.
    """
    venue_id: str

    @property
    def is_active(self) -> bool:
        return False

    @property
    def is_bookable(self) -> bool:
        return False


Resolution = Union[Venue, Unresolved]


class RegistryError(RuntimeError):
    pass


_cache: Optional[dict] = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(SNAPSHOT_PATH) as fh:
            raw = json.load(fh)
    except FileNotFoundError as e:
        raise RegistryError(f"venue snapshot missing at {SNAPSHOT_PATH}") from e

    venues = {}
    for row in raw.get("venues", []):
        v = Venue(
            facility_id=int(row["facility_id"]),
            platform=row["platform"],
            slug=row["slug"],
            name=row["name"],
            status=row["status"],
            fetcher=row.get("fetcher"),
            booking_enabled=bool(row["booking_enabled"]),
        )
        venues[v.facility_id] = v
    if not venues:
        raise RegistryError("venue snapshot contains no venues")
    _cache = venues
    return _cache


def resolve(venue_id) -> Resolution:
    """
    Resolve any venue reference -- an int, or the text `venue_id` stored on
    favorites / created_sessions / booked_sessions.

    Never raises for an unknown id and never substitutes another venue.
    """
    if venue_id is None:
        return Unresolved("")
    raw = str(venue_id).strip()
    if not raw or not raw.isdigit():
        return Unresolved(raw)
    return _load().get(int(raw)) or Unresolved(raw)


def get_venue(facility_id: int) -> Venue:
    """A registered venue of any status. Raises if there is none."""
    v = _load().get(int(facility_id))
    if v is None:
        raise RegistryError(f"facility {facility_id} is not in the registry")
    return v


def get_bookable_venue(facility_id: int) -> Venue:
    """
    The only correct entry point for pricing and booking.

    Fails closed. A delisted venue, or an active one whose booking is not
    enabled, raises here rather than proceeding with a slug that would send
    the request somewhere plausible but wrong.
    """
    v = get_venue(facility_id)
    if v.status == "delisted":
        raise RegistryError(f"facility {facility_id} ({v.name}) is delisted")
    if not v.booking_enabled:
        raise RegistryError(
            f"facility {facility_id} ({v.name}) is not booking-enabled")
    return v


def active_venues() -> list[Venue]:
    return [v for v in sorted(_load().values(), key=lambda x: x.facility_id)
            if v.status == "active"]


def bookable_venues() -> list[Venue]:
    return [v for v in active_venues() if v.booking_enabled]


def venues_for_fetcher(fetcher: str) -> list[Venue]:
    """Which venues a given scraper is responsible for."""
    return [v for v in active_venues() if v.fetcher == fetcher]


def slug_for(facility_id: int) -> str:
    """
    Replaces `SLUG_MAP.get(facility_id, "nplpickleball")`.

    That default is why five venues absent from SLUG_MAP sent their price and
    booking requests carrying another facility's Referer. There is no default
    here on purpose.
    """
    return get_venue(facility_id).slug


def all_facility_ids() -> set[int]:
    return set(_load().keys())


def validate_facility_keys(name: str, facility_ids, *, require_bookable=False):
    """
    Check a facility-keyed constant that lives OUTSIDE the registry -- e.g.
    booking_server's FALLBACK_PLANS, FALLBACK_CLINIC_IDS,
    INDIVIDUAL_PRICE_FACILITIES. Those are pricing capability, not venue
    identity, so they stay where they are; but an entry for a facility that
    is not registered, or is delisted, is a defect.

    Returns a list of human-readable problems; empty means clean.
    """
    problems = []
    for fid in facility_ids:
        try:
            v = get_venue(int(fid))
        except RegistryError:
            problems.append(f"{name}: facility {fid} is not in the registry")
            continue
        if v.status == "delisted":
            problems.append(f"{name}: facility {fid} ({v.name}) is delisted")
        elif require_bookable and not v.booking_enabled:
            problems.append(
                f"{name}: facility {fid} ({v.name}) is not booking-enabled")
    return problems
