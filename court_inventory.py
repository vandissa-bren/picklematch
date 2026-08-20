"""
court_inventory.py -- a facility's physical court inventory, discovered
independently of whether anything is free.

This is the piece 2A could not have. Every court enumeration in this project
went through available_courts, which is availability-filtered by definition,
so a fully booked court was simply absent and "no such court" could not be
told apart from "not free then". Both came back as court_not_found.

    /api/facilities/{id}/courts   the inventory. Returns every court with
                                  id, name, surface and archived, regardless
                                  of bookings.
    /api/facilities/{id}/court_types (UNFILTERED)
                                  the surfaces, classified by court_surfaces

Joined on `surface`, which both payloads carry. The join is the point: the
courts endpoint returns every court at the facility, including ones on
surfaces that are not court-hire inventory at all, so taking it wholesale
would put saunas and meeting rooms in the court list.

    court_types (unfiltered)
            v
    surface classification
        COURT     -> contributes inventory
        NON_COURT -> excluded
        ALTERNATE -> excluded from capacity, carried for checkout
        UNKNOWN   -> excluded, loud diagnostic
            v
    /courts, joined on surface, archived dropped
            v
    canonical court_id + court_name
            v
    availability, asked separately

Two rules this exists to enforce:

    one physical court contributes one unit of inventory, however many PBP
    surfaces expose it

    uncertainty removes availability; it never manufactures capacity

Do NOT pass kind=reservation when discovering surfaces. It hides
members_only at Pickleplex and The Rally and training_court at Picklezone,
while available_courts still serves courts on those surfaces.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import court_surfaces as cs

logger = logging.getLogger(__name__)

INVENTORY_TTL_SECONDS = 3600


class InventoryError(RuntimeError):
    """Inventory could not be established. Never a reason to guess."""


class CourtNotFound(LookupError):
    """The id is not a court at this facility. Distinct from unavailable."""


@dataclass(frozen=True)
class Court:
    id: int
    name: str
    surface: str

    def __str__(self) -> str:
        return f"{self.id} ({self.name})"


@dataclass
class FacilityInventory:
    facility_id: int
    fetched_at: float = 0.0
    courts: list[Court] = field(default_factory=list)
    alternate: list[Court] = field(default_factory=list)
    excluded: list[Court] = field(default_factory=list)
    archived: list[Court] = field(default_factory=list)
    unknown_surfaces: list[str] = field(default_factory=list)

    @property
    def ids(self) -> set[int]:
        return {c.id for c in self.courts}

    def get(self, court_id) -> Court:
        """
        The canonical court for an id, or CourtNotFound.

        Answers existence only. Whether it is free at a given time is a
        separate question and a separate answer -- that separation is the
        whole reason this module exists.
        """
        try:
            wanted = int(str(court_id).strip())
        except (TypeError, ValueError):
            raise CourtNotFound(f"court_not_found: {court_id!r} is not a court id")
        for c in self.courts:
            if c.id == wanted:
                return c
        # Say so explicitly when the id is real but deliberately excluded --
        # otherwise a members_only id looks identical to a typo.
        for c in self.alternate:
            if c.id == wanted:
                raise CourtNotFound(
                    f"court_not_found: court {wanted} ({c.name}) is on the "
                    f"'{c.surface}' surface, which re-exposes courts counted "
                    f"elsewhere and is not bookable inventory")
        for c in self.archived:
            if c.id == wanted:
                raise CourtNotFound(
                    f"court_not_found: court {wanted} ({c.name}) is archived")
        raise CourtNotFound(
            f"court_not_found: court {wanted} is not a court at facility "
            f"{self.facility_id}")

    def by_name(self, court_name: str) -> Court:
        """
        Transitional, for clients still sending names. Ambiguity raises
        rather than resolving to an arbitrary match -- names are
        presentation, ids are identity.
        """
        want = (court_name or "").strip().lower()
        hits = [c for c in self.courts if c.name.strip().lower() == want]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise CourtNotFound(
                f"court_not_found: '{court_name}' is ambiguous at facility "
                f"{self.facility_id} ({len(hits)} courts share it); send court_id")
        raise CourtNotFound(
            f"court_not_found: no court named '{court_name}' at facility "
            f"{self.facility_id}")

    def diagnostic(self) -> str | None:
        return (f"facility {self.facility_id}: UNKNOWN_SURFACE "
                f"{self.unknown_surfaces} -- contributing no courts until "
                f"classified. Review surfaces.json.") if self.unknown_surfaces else None


def build_inventory(facility_id: int, court_types: list[dict],
                    courts_payload: list[dict]) -> FacilityInventory:
    """
    Pure. Takes the two raw payloads so it is testable against recorded
    responses and cannot behave differently in a test than in production.
    """
    res = cs.resolve_surfaces(facility_id, court_types)
    court_surfaces = set(res.court)
    alt_surfaces = set(res.alternate)

    inv = FacilityInventory(facility_id=facility_id,
                            unknown_surfaces=list(res.unknown))
    seen: set[int] = set()
    for row in courts_payload or []:
        if not row or row.get("id") is None:
            continue
        try:
            cid = int(row["id"])
        except (TypeError, ValueError):
            continue
        if cid in seen:          # one physical court, one record
            continue
        seen.add(cid)
        court = Court(id=cid, name=(row.get("name") or "").strip(),
                      surface=row.get("surface") or "")
        if row.get("archived"):
            inv.archived.append(court)
        elif court.surface in court_surfaces:
            inv.courts.append(court)
        elif court.surface in alt_surfaces:
            inv.alternate.append(court)
        else:
            # Classify the court's OWN surface rather than relying on it
            # having appeared in court_types. /courts can contain surfaces
            # court_types omits -- The Rally's function_room ("The Rally
            # Lounge") is in the inventory payload but in neither the
            # filtered nor the unfiltered court_types, so the estate census
            # never saw it. Deciding by court_types membership alone would
            # drop such a court into `excluded` with no diagnostic, which is
            # the silent failure this module exists to end.
            klass = cs.classify(court.surface)
            if klass == cs.UNKNOWN:
                inv.excluded.append(court)
                if court.surface and court.surface not in inv.unknown_surfaces:
                    inv.unknown_surfaces.append(court.surface)
            elif klass == cs.ALTERNATE:
                inv.alternate.append(court)
            else:
                inv.excluded.append(court)

    if not inv.courts:
        raise InventoryError(
            f"facility {facility_id} has no court inventory "
            f"(court surfaces={sorted(court_surfaces)}, "
            f"alternate={len(inv.alternate)}, excluded={len(inv.excluded)}, "
            f"archived={len(inv.archived)}, unknown={res.unknown}) "
            f"-- refusing to guess")
    inv.courts.sort(key=lambda c: c.id)
    inv.unknown_surfaces.sort()
    return inv


async def fetch_inventory(session, cookies, facility_id: int,
                          slug: str) -> FacilityInventory:
    """Live inventory. Two GETs, neither dependent on a date or a time."""
    hdrs = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://app.playbypoint.com/book/{slug}"}
    base = f"https://app.playbypoint.com/api/facilities/{facility_id}"

    # Unfiltered on purpose -- see the module docstring.
    rt = await session.get(f"{base}/court_types", cookies=cookies, headers=hdrs)
    if rt.status_code != 200:
        raise InventoryError(
            f"facility {facility_id}: court_types HTTP {rt.status_code}")
    rc = await session.get(f"{base}/courts", cookies=cookies, headers=hdrs)
    if rc.status_code != 200:
        raise InventoryError(
            f"facility {facility_id}: courts HTTP {rc.status_code}")

    types = rt.json() or []
    courts = rc.json() or []
    if not isinstance(courts, list):
        raise InventoryError(
            f"facility {facility_id}: courts returned {type(courts).__name__}, "
            f"expected a list")
    return build_inventory(facility_id, types, courts)


# ── cache ───────────────────────────────────────────────────────────────────
#
# Inventory is date and time independent, so fetching it per request is two
# GETs that return the same answer until a venue adds or retires a court.
# One cache lives here rather than one per consumer: four slightly different
# TTL caches would recreate, one level down, exactly the drift that the
# venue registry and surface classification were built to remove.
#
# Deliberately NOT in availability_cache. Inventory state and availability
# state are different things, and sharing a row would couple them and add a
# synchronisation failure mode that nothing yet justifies.

_cache: dict[int, FacilityInventory] = {}


def cache_state() -> list[dict]:
    """For diagnostics -- a stale inventory should never be invisible."""
    now = time.time()
    return [{"facility_id": fid, "courts": len(inv.courts),
             "age_seconds": round(now - inv.fetched_at),
             "stale": (now - inv.fetched_at) > INVENTORY_TTL_SECONDS}
            for fid, inv in sorted(_cache.items())]


def clear_cache(facility_id: int | None = None) -> None:
    if facility_id is None:
        _cache.clear()
    else:
        _cache.pop(facility_id, None)


async def get_inventory(session, cookies, facility_id: int, slug: str, *,
                        ttl: int = INVENTORY_TTL_SECONDS,
                        force: bool = False) -> FacilityInventory:
    """
    Cached inventory. The single entry point every consumer should use.

    Failure rule, mirroring the scraper price fix: a refresh that fails must
    never replace a known-good inventory with nothing.

        cached and fresh              -> return it
        cached and stale, refresh ok  -> return the new one
        cached and stale, refresh bad -> return the CACHED one, log loudly.
                                         A transient PBP error must not empty
                                         a venue's court list.
        not cached, fetch fails       -> raise. No 'pickleball' fallback, no
                                         empty inventory that would read as
                                         'this venue has no courts'.
    """
    cached = _cache.get(facility_id)
    age = time.time() - cached.fetched_at if cached else None
    if cached and not force and age is not None and age < ttl:
        return cached

    try:
        inv = await fetch_inventory(session, cookies, facility_id, slug)
        inv.fetched_at = time.time()
        _cache[facility_id] = inv
        if inv.unknown_surfaces:
            logger.warning(inv.diagnostic())
        return inv
    except Exception as e:
        if cached:
            logger.warning(
                "facility %s: inventory refresh failed (%s); keeping "
                "last-known-good inventory of %d courts, age %ds",
                facility_id, e, len(cached.courts), round(age or 0))
            return cached
        raise InventoryError(
            f"facility {facility_id}: no cached inventory and refresh failed "
            f"({e}) -- refusing to guess") from e
