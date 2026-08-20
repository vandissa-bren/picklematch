"""
court_surfaces.py -- the single answer to "which PBP surfaces are
court-hire inventory at this facility?"

Replaces three hardcoded per-venue surface maps and one name heuristic:

    booking_server.SURFACE_MAP          2 venues, everything else defaulted
                                        to "pickleball"
    api_server.VENUE_SURFACES           3 venues, one entry malformed
    fetch_court_blocks.VENUE_SURFACES   3 venues
    'pickle' in surface.lower()         used by book_court and the scraper

Why this is not SURFACE_MAP again
---------------------------------
Those maps answered "which surfaces does venue X have?" -- a question PBP can
answer itself, and which needed hand-maintenance per venue. Getting it wrong
was invisible: PBP answers an unknown surface with an empty 200, which looks
exactly like "fully booked".

This answers a different question: "is this surface, wherever it appears,
court-hire inventory?" That is a semantic judgement PBP does not expose, so
it has to live somewhere -- but it is one global list keyed by surface, not
per-venue selections, and an unrecognised value raises instead of silently
defaulting to "pickleball".

Discovery stays with PBP. Classification stays with us.

Four states, and the difference between the last two matters:

    COURT       independently contributes physical court inventory
    NON_COURT   deliberately excluded; a real resource that is not a court
    ALTERNATE   a court-hire surface representing EXISTING physical courts
                through another booking or entitlement channel; contributes
                no capacity, but is relevant to pricing and checkout
    UNKNOWN     never seen before; contributes nothing and must be reviewed

The governing invariant:

    one physical court contributes one unit of inventory regardless of how
    many PBP surfaces expose it

which is why ALTERNATE exists as its own state rather than being folded into
NON_COURT. members_only IS court hire -- it is how a member books the same
physical court and receives their entitlement -- it simply is not additional
capacity. Calling it NON_COURT would lose that, and calling it COURT would
double the court count at The Rally and Pickleplex.

UNKNOWN is not NON_COURT. A surface we have never seen is evidence that
PBP's resource model has changed, and it should be loud. NON_COURT is a
decision already taken and is silent.

The invariant, which every caller depends on:

    uncertainty can remove availability, but can never manufacture capacity.

A new PBP surface therefore reduces what we show until someone classifies
it. That is the correct trade: showing fewer courts is recoverable, selling
a sauna slot as court hire is not.

Do NOT filter discovery with kind=reservation. It hides members_only at
Pickleplex and The Rally and training_court at Picklezone, while
available_courts will still serve courts on those surfaces -- PBP
contradicting itself. Enumerate unfiltered and classify here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

CLASSIFICATION_PATH = os.environ.get(
    "SURFACE_CLASSIFICATION_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "surfaces.json"),
)

COURT = "COURT"
NON_COURT = "NON_COURT"
ALTERNATE = "ALTERNATE"
UNKNOWN = "UNKNOWN"

_data: dict | None = None


class SurfaceError(RuntimeError):
    pass


def _load() -> dict:
    global _data
    if _data is None:
        try:
            with open(CLASSIFICATION_PATH) as fh:
                _data = json.load(fh)
        except FileNotFoundError as e:
            raise SurfaceError(
                f"surface classification missing at {CLASSIFICATION_PATH}") from e
        if not _data.get("classifications"):
            raise SurfaceError("surface classification file is empty")
    return _data


def classify(surface: str | None) -> str:
    """COURT / NON_COURT / ALTERNATE / UNKNOWN. Never raises for an unseen value."""
    if not surface:
        return UNKNOWN
    entry = _load()["classifications"].get(surface.strip())
    return entry["class"] if entry else UNKNOWN


def evidence_for(surface: str) -> dict:
    return _load()["classifications"].get(surface, {})


def census() -> set[str]:
    """Surfaces observed across the estate at the last review."""
    return set(_load()["census"]["observed"])


def all_of(cls: str) -> list[str]:
    return sorted(s for s, e in _load()["classifications"].items()
                  if e["class"] == cls)


@dataclass
class SurfaceResolution:
    """
    What a facility's surfaces resolve to.

    Callers use `court` for inventory and must REPORT `unknown` rather than
    ignore it -- a silently dropped surface is the failure this replaces.
    `alternate` is not a problem to report: it is a known, understood
    surface that deliberately carries no capacity, and it is where an
    entitlement or checkout-option lookup would begin.
    """
    facility_id: int
    court: list[str] = field(default_factory=list)
    non_court: list[str] = field(default_factory=list)
    alternate: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.unknown)

    def diagnostic(self) -> str | None:
        """A line worth logging loudly, or None when everything is classified."""
        if not self.needs_review:
            return None
        bits = []
        if self.unknown:
            bits.append(f"UNKNOWN_SURFACE {self.unknown} -- never seen before, "
                        f"contributing no courts until classified")

        return f"facility {self.facility_id}: " + "; ".join(bits)


def resolve_surfaces(facility_id: int, court_types: list[dict]) -> SurfaceResolution:
    """
    Classify the surfaces PBP reports for a facility.

    `court_types` is the UNFILTERED /api/facilities/{id}/court_types payload.
    Pure: no network, so it is testable against a recorded payload and cannot
    fail differently in a test than in production.
    """
    res = SurfaceResolution(facility_id=facility_id)
    for t in court_types or []:
        surface = (t or {}).get("surface")
        if not surface:
            continue
        bucket = {COURT: res.court, NON_COURT: res.non_court,
                  ALTERNATE: res.alternate, UNKNOWN: res.unknown}[classify(surface)]
        if surface not in bucket:
            bucket.append(surface)
    for lst in (res.court, res.non_court, res.alternate, res.unknown):
        lst.sort()
    return res


def court_surfaces(facility_id: int, court_types: list[dict]) -> list[str]:
    """
    Just the surfaces that contribute inventory.

    Raises when a facility resolves to no court surfaces at all: that means
    either PBP returned nothing or every surface it returned is unclassified,
    and both are conditions to stop on. The old code's response to the same
    situation was to ask for "pickleball" and get an empty list back, which
    is indistinguishable from a fully booked venue -- which is how The Rally
    went unpriceable without anyone noticing.
    """
    res = resolve_surfaces(facility_id, court_types)
    if not res.court:
        raise SurfaceError(
            f"facility {facility_id} resolved to no court surfaces "
            f"(non_court={res.non_court}, alternate={res.alternate}, "
            f"unknown={res.unknown}) -- refusing to guess")
    return res.court
