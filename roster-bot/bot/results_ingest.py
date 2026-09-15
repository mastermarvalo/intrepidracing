"""
Sheet/CSV parsing for race results.

Deliberately outside `bot/market/`: this is plumbing that turns a
spreadsheet into `race_results` rows, not league business logic, so it
sits outside the ADR-001 magic-number guard. Nothing here decides what a
result is *worth* — that stays in `bot/market/results.py` and the
`position_scores` table.

The parser is tolerant on input and strict on output. Header names are
matched case-insensitively against a set of aliases, so a league can
label the column "Pos", "Finish", or "Finishing Position" and it still
works, but anything ambiguous or unparseable comes back as a numbered
error rather than being silently coerced to zero. A results import that
half-succeeds is worse than one that refuses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# Column aliases, lowercase. First match wins.
_ALIASES: dict[str, tuple[str, ...]] = {
    "driver": ("driver", "name", "member", "racer", "driver name"),
    "finish": ("finish", "pos", "position", "finishing position", "result", "place"),
    "grid": ("grid", "start", "quali", "qualifying", "grid position", "starting position"),
    "dnf": ("dnf", "retired", "retirement"),
    "dns": ("dns", "no show", "noshow", "absent", "did not start"),
    "fastest_lap": ("fastest lap", "fl", "fastest", "fastestlap"),
    "driver_of_day": ("dotd", "driver of the day", "driver of day", "dod"),
    "incidents": ("incidents", "incident points", "penalties", "penalty points"),
    "note": ("note", "notes", "comment", "comments"),
}

# Cell values that mean "yes" for a boolean column. Anything else, including
# an empty cell, means no.
_TRUTHY = {"y", "yes", "true", "1", "x", "✓", "✔", "t"}

# Cell values that mean "this driver did not finish" when they appear in
# the finishing-position column instead of a number.
_DNF_TOKENS = {"dnf", "ret", "retired", "dq", "dsq", "disqualified"}
_DNS_TOKENS = {"dns", "-", "n/a", "na", "absent", "no show"}


@dataclass
class ParsedResult:
    """One driver's parsed row, before driver-id resolution."""

    driver_name: str
    finish_position: int | None = None
    grid_position: int | None = None
    dnf: bool = False
    dns: bool = False
    fastest_lap: bool = False
    driver_of_day: bool = False
    incident_points: Decimal = Decimal(0)
    note: str | None = None
    source_row: int = 0


@dataclass
class ParseOutcome:
    rows: list[ParsedResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _normalize_header(cell: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", cell.strip().lower()).strip()


def map_columns(header: list[str]) -> tuple[dict[str, int], list[str]]:
    """
    Map logical column names to their index in the header row.

    Returns (mapping, errors). Only `driver` is mandatory — a league that
    does not track, say, incident points simply omits the column and
    those observations stay zero.
    """
    normalized = [_normalize_header(c) for c in header]
    mapping: dict[str, int] = {}
    for logical, aliases in _ALIASES.items():
        for idx, cell in enumerate(normalized):
            if cell in aliases:
                mapping[logical] = idx
                break

    errors: list[str] = []
    if "driver" not in mapping:
        errors.append(
            "No driver column found. Expected one of: "
            + ", ".join(_ALIASES["driver"])
        )
    if "finish" not in mapping:
        errors.append(
            "No finishing-position column found. Expected one of: "
            + ", ".join(_ALIASES["finish"])
        )
    return mapping, errors


def _cell(row: list[str], mapping: dict[str, int], key: str) -> str:
    idx = mapping.get(key)
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def _parse_bool(row: list[str], mapping: dict[str, int], key: str) -> bool:
    return _cell(row, mapping, key).lower() in _TRUTHY


def _parse_position(raw: str) -> tuple[int | None, bool, bool, str | None]:
    """
    Parse a position cell into (position, dnf, dns, error).

    Accepts a plain number, a "P4"-style prefix, or one of the textual
    retirement/absence tokens.
    """
    value = raw.strip().lower()
    if not value:
        return None, False, True, None
    if value in _DNF_TOKENS:
        return None, True, False, None
    if value in _DNS_TOKENS:
        return None, False, True, None
    cleaned = re.sub(r"^[pP#]", "", value)
    cleaned = re.sub(r"(st|nd|rd|th)$", "", cleaned)
    try:
        pos = int(cleaned)
    except ValueError:
        return None, False, False, f"could not read position {raw!r}"
    if pos <= 0:
        return None, False, False, f"position must be positive, got {raw!r}"
    return pos, False, False, None


def parse_results(values: list[list[str]]) -> ParseOutcome:
    """
    Parse a sheet range (first row = header) into ParsedResult rows.

    Duplicate driver names are rejected: two rows for one driver in one
    round is always a data-entry mistake, and picking one silently would
    hide it.
    """
    outcome = ParseOutcome()
    if not values:
        outcome.errors.append("The sheet range returned no rows.")
        return outcome
    if len(values) < 2:
        outcome.errors.append("The sheet range has a header but no data rows.")
        return outcome

    mapping, header_errors = map_columns(values[0])
    if header_errors:
        outcome.errors.extend(header_errors)
        return outcome

    seen: dict[str, int] = {}
    for offset, raw_row in enumerate(values[1:], start=2):
        if not any(c.strip() for c in raw_row):
            continue  # blank spacer row

        name = _cell(raw_row, mapping, "driver")
        if not name:
            outcome.errors.append(f"Row {offset}: no driver name.")
            continue

        key = name.casefold()
        if key in seen:
            outcome.errors.append(
                f"Row {offset}: duplicate entry for {name!r} "
                f"(already on row {seen[key]})."
            )
            continue
        seen[key] = offset

        finish, dnf, dns, err = _parse_position(_cell(raw_row, mapping, "finish"))
        if err:
            outcome.errors.append(f"Row {offset} ({name}): {err}")
            continue

        grid: int | None = None
        grid_raw = _cell(raw_row, mapping, "grid")
        if grid_raw:
            grid, _, _, grid_err = _parse_position(grid_raw)
            if grid_err:
                outcome.errors.append(f"Row {offset} ({name}): {grid_err}")
                continue

        incidents = Decimal(0)
        incidents_raw = _cell(raw_row, mapping, "incidents")
        if incidents_raw:
            try:
                incidents = Decimal(incidents_raw)
            except InvalidOperation:
                outcome.errors.append(
                    f"Row {offset} ({name}): could not read incident points "
                    f"{incidents_raw!r}."
                )
                continue
            if incidents < 0:
                outcome.errors.append(
                    f"Row {offset} ({name}): incident points cannot be negative."
                )
                continue

        # An explicit DNF/DNS column overrides a blank position cell.
        dnf = dnf or _parse_bool(raw_row, mapping, "dnf")
        dns = dns or _parse_bool(raw_row, mapping, "dns")
        if dnf:
            dns = False
            finish = None
        if dns:
            finish = None

        outcome.rows.append(
            ParsedResult(
                driver_name=name,
                finish_position=finish,
                grid_position=grid,
                dnf=dnf,
                dns=dns,
                fastest_lap=_parse_bool(raw_row, mapping, "fastest_lap"),
                driver_of_day=_parse_bool(raw_row, mapping, "driver_of_day"),
                incident_points=incidents,
                note=_cell(raw_row, mapping, "note") or None,
                source_row=offset,
            )
        )

    if not outcome.rows and not outcome.errors:
        outcome.errors.append("No usable data rows found.")

    # A round cannot have two winners or two poles; catching it here beats
    # discovering it in a published market.
    _check_unique_positions(outcome)
    return outcome


def _check_unique_positions(outcome: ParseOutcome) -> None:
    for label, getter in (
        ("finishing position", lambda r: r.finish_position),
        ("grid position", lambda r: r.grid_position),
    ):
        seen: dict[int, str] = {}
        for row in outcome.rows:
            pos = getter(row)
            if pos is None:
                continue
            if pos in seen:
                outcome.errors.append(
                    f"Duplicate {label} P{pos}: {seen[pos]} and {row.driver_name}."
                )
            else:
                seen[pos] = row.driver_name


def resolve_drivers(
    rows: list[ParsedResult], roster: dict[str, int]
) -> tuple[list[dict], list[str]]:
    """
    Match parsed rows to driver ids.

    `roster` maps casefolded display name -> driver id. Unmatched names
    are reported rather than skipped: a typo'd name would otherwise mean
    a driver silently receives no valuation movement for the round.
    """
    resolved: list[dict] = []
    errors: list[str] = []
    for row in rows:
        driver_id = roster.get(row.driver_name.casefold())
        if driver_id is None:
            errors.append(
                f"Row {row.source_row}: no driver named {row.driver_name!r} "
                "in this tier."
            )
            continue
        resolved.append(
            {
                "driver_id": driver_id,
                "finish_position": row.finish_position,
                "grid_position": row.grid_position,
                "dnf": row.dnf,
                "dns": row.dns,
                "fastest_lap": row.fastest_lap,
                "driver_of_day": row.driver_of_day,
                "incident_points": row.incident_points,
                "note": row.note,
            }
        )
    return resolved, errors
