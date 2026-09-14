"""
Discord protocol limits + rendering constants.

Lives outside `bot/market/` and `bot/contracts/` on purpose: these
values are external infrastructure (fixed by the Discord API) or
rendering choices, not league business logic. Isolating them here
keeps the ADR-001 magic-number guard focused on money and contract
rules while still giving the render layer a single place to look up
"what's Discord's cap on an embed field?"

Update carefully — most of these are protocol limits, not preferences.
Bumping `MARKET_PAGE_SIZE` is a UX call; bumping `EMBED_FIELD_VALUE_MAX`
is a fast-track to broken embeds.
"""

# ── Discord API hard limits (do NOT change without a Discord change) ──

# https://discord.com/developers/docs/resources/message#embed-object-embed-limits
EMBED_TITLE_MAX = 256
EMBED_DESCRIPTION_MAX = 4096
EMBED_FIELDS_MAX = 25
EMBED_FIELD_NAME_MAX = 256
EMBED_FIELD_VALUE_MAX = 1024
EMBED_FOOTER_MAX = 2048
EMBED_AUTHOR_MAX = 256
EMBED_TOTAL_MAX = 6000

# https://discord.com/developers/docs/resources/message#create-message
MESSAGE_CONTENT_MAX = 2000

# ── Rendering choices (safe to tune within reason) ────────────────────

# How wide a rendered line may be before we consider it likely to wrap
# awkwardly on mobile. Chosen to match the ruff line-length used in the
# rest of the repo — it's an aesthetic bound, not a hard cap.
RENDER_LINE_WIDTH = 100

# Number of drivers per page in `/market view` (and the paginated
# market boards). Two lines per driver → ~20 lines per page → comfortable
# on mobile without excessive scrolling to reach the pagination footer.
MARKET_PAGE_SIZE = 10

# `/market movers` shows the top N in each direction.
MOVERS_PER_DIRECTION = 5

# `/market dashboard` shows the top N per tier in the cross-tier summary.
DASHBOARD_PER_TIER = 3

# Last-N-runs shown in the driver card's trend line.
DRIVER_TREND_ENTRIES = 5

# How many valuation runs `/market-admin valuation list` returns.
VALUATION_LIST_LIMIT = 20
