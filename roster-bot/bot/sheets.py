"""
Google Sheets fetch and embed builder.

Parses spreadsheet URLs, calls the Sheets API v4, and returns a Discord
embed formatted as a monospace table. All API errors produce an error embed
instead of raising so callers can always display something.

Auth priority:
  1. GOOGLE_SERVICE_ACCOUNT_JSON — path to a service account JSON file, OR
     the raw JSON content itself (useful for environment-variable secrets).
     The service account must have the sheet shared with its email address.
  2. GOOGLE_SHEETS_API_KEY — simple API key for publicly shared sheets.
"""

import asyncio
import json
import logging
import os
import re
from datetime import UTC, datetime

import aiohttp
import discord

log = logging.getLogger(__name__)

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets/{id}/values/{range}"
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
_COL_MAX = 22
_TABLE_CHAR_LIMIT = 3900
_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Module-level cache so we reuse the credentials object across requests.
_sa_creds = None


def parse_sheet_id(url: str) -> str | None:
    """Extract the spreadsheet ID from a Google Sheets URL. Returns None on failure."""
    m = _SHEET_ID_RE.search(url)
    return m.group(1) if m else None


def _truncate(text: str, maxlen: int) -> str:
    if len(text) <= maxlen:
        return text
    return text[: maxlen - 1] + "…"


def _build_table(rows: list[list[str]]) -> str:
    if not rows:
        return "*No data.*"

    ncols = max(len(r) for r in rows)
    padded = [r + [""] * (ncols - len(r)) for r in rows]

    widths = [
        min(_COL_MAX, max(len(padded[ri][ci]) for ri in range(len(padded))))
        for ci in range(ncols)
    ]

    def fmt_row(r: list[str]) -> str:
        return "  ".join(_truncate(cell, widths[ci]).ljust(widths[ci]) for ci, cell in enumerate(r))

    header = fmt_row(padded[0])
    separator = "  ".join("-" * widths[ci] for ci in range(ncols))
    data_rows = [fmt_row(r) for r in padded[1:]]

    lines: list[str] = [header, separator]
    hidden = 0
    for row_line in data_rows:
        candidate = "```\n" + "\n".join(lines + [row_line]) + "\n```"
        if len(candidate) > _TABLE_CHAR_LIMIT:
            hidden = len(data_rows) - (len(lines) - 2)
            break
        lines.append(row_line)

    table = "```\n" + "\n".join(lines) + "\n```"
    if hidden:
        table += f"\n*… {hidden} row(s) not shown*"
    return table


def _error_embed(message: str) -> discord.Embed:
    return discord.Embed(
        title="Stat Board — Error",
        description=message,
        color=discord.Color.red(),
        timestamp=datetime.now(UTC),
    )


def _refresh_service_account() -> dict[str, str] | None:
    """Synchronous helper — runs in a thread executor. Returns auth headers or None."""
    global _sa_creds

    json_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not json_env:
        return None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        if _sa_creds is None:
            if json_env.strip().startswith("{"):
                info = json.loads(json_env)
                _sa_creds = service_account.Credentials.from_service_account_info(
                    info, scopes=_SCOPES
                )
            else:
                _sa_creds = service_account.Credentials.from_service_account_file(
                    json_env, scopes=_SCOPES
                )

        if not _sa_creds.valid:
            _sa_creds.refresh(Request())

        return {"Authorization": f"Bearer {_sa_creds.token}"}
    except Exception as exc:
        log.error("Service account auth failed: %s", exc)
        return None


async def _get_request_kwargs() -> tuple[dict, dict] | None:
    """Return (headers, params) for the Sheets API request, or None if no auth configured."""
    loop = asyncio.get_event_loop()
    headers = await loop.run_in_executor(None, _refresh_service_account)
    if headers is not None:
        return headers, {}

    api_key = os.getenv("GOOGLE_SHEETS_API_KEY")
    if api_key:
        return {}, {"key": api_key}

    return None


async def fetch_and_build_embed(
    title: str, sheet_id: str, sheet_range: str
) -> discord.Embed:
    """Fetch sheet values and build a Discord embed.

    Never raises — returns an error embed on failure.
    """
    auth = await _get_request_kwargs()
    if auth is None:
        return _error_embed(
            "No Google Sheets credentials configured.\n"
            "Set `GOOGLE_SERVICE_ACCOUNT_JSON` (path to service account JSON) **or** "
            "`GOOGLE_SHEETS_API_KEY` in your `.env` file."
        )

    headers, params = auth
    url = _SHEETS_API.format(id=sheet_id, range=sheet_range)

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 403:
                    return _error_embed(
                        f"Access denied (403) for sheet `{sheet_id}`.\n"
                        "• **Service account**: share the sheet with the "
                        "service account email (Viewer).\n"
                        "• **API key**: make sure the sheet is shared "
                        "publicly (Viewer)."
                    )
                if resp.status == 404:
                    return _error_embed(
                        f"Sheet not found (404) for ID `{sheet_id}` / range `{sheet_range}`. "
                        "Check the URL and range name."
                    )
                if resp.status != 200:
                    text = await resp.text()
                    return _error_embed(
                        f"Sheets API returned HTTP {resp.status}.\n```\n{text[:300]}\n```"
                    )
                data = await resp.json()
    except aiohttp.ClientError as exc:
        log.warning("Sheets fetch error for %s/%s: %s", sheet_id, sheet_range, exc)
        return _error_embed(f"Network error fetching sheet: {exc}")

    raw_rows: list[list[str]] = data.get("values", [])
    if not raw_rows:
        table = "*The sheet returned no data.*"
    else:
        table = _build_table(raw_rows)

    embed = discord.Embed(
        description=f"# __{title}__\n{table}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(UTC),
    )
    embed.set_footer(text=f"Range: {sheet_range}")
    return embed
