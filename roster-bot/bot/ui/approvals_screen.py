"""
The commissioner approval queue as an interactive screen.

Previously the panel showed two counts and told the admin to go type
`/market-admin approve offer_id:<id>` — without listing the ids, so they
had to go find them. This screen lists each pending item with its terms
and gives it Approve / Reject buttons.

Every action routes through `bot.approvals`, the same module the slash
commands use, so the audit trail and role side-effects are identical
whichever route a commissioner takes.
"""

from __future__ import annotations

import discord

from bot import approvals
from bot.market.money import format_money
from bot.ui.base import (
    COLOR_OK,
    COLOR_WARN,
    SELECT_MAX_OPTIONS,
    AdminOwnedView,
    BackButton,
    BackCallback,
    report_error,
    truncate_field,
)

# Reserve nothing: the select is the only element competing for options,
# and the queue read is already limited to this many rows.
_QUEUE_LIMIT = SELECT_MAX_OPTIONS

_OFFER = "offer"
_TRADE = "trade"


def build_approvals_embed(queue: approvals.PendingQueue) -> discord.Embed:
    """The queue itself — ids visible, so nothing has to be looked up."""
    if queue.season_id is None:
        return discord.Embed(
            title="📋 Awaiting approval",
            description="No active season, so nothing can be pending.",
            color=COLOR_WARN,
        )

    if queue.is_empty:
        return discord.Embed(
            title="📋 Awaiting approval",
            description="Queue is clear — nothing is waiting on you.",
            color=COLOR_OK,
        )

    embed = discord.Embed(
        title="📋 Awaiting approval",
        description=(
            f"**{len(queue.offers)}** contract offer(s) and "
            f"**{len(queue.trades)}** trade(s) in **{queue.season_name}**.\n"
            "Oldest first. Pick one below to review it."
        ),
        color=COLOR_WARN,
    )

    if queue.offers:
        lines = [
            f"`{o.offer_id}` · **{o.driver_name}** → {o.team_name} "
            f"(`{o.tier_code}`) · {format_money(o.salary)}/season × "
            f"{o.term_seasons}"
            for o in queue.offers
        ]
        embed.add_field(
            name="Contract offers",
            value=truncate_field("\n".join(lines)),
            inline=False,
        )

    if queue.trades:
        lines = [
            f"`{t.trade_id}` · {t.proposing_team_name} ⇄ {t.other_team_name} "
            f"· {t.item_count} contract(s)"
            for t in queue.trades
        ]
        embed.add_field(
            name="Trades", value=truncate_field("\n".join(lines)), inline=False
        )

    embed.set_footer(text="Equivalent commands: /market-admin approve · approve-trade")
    return embed


def _build_offer_detail(offer: approvals.PendingOffer) -> discord.Embed:
    embed = discord.Embed(
        title=f"📝 Offer #{offer.offer_id}",
        description=(
            f"**{offer.driver_name}** → **{offer.team_name}** "
            f"(tier `{offer.tier_code}`)"
        ),
        color=COLOR_WARN,
    )
    embed.add_field(name="Salary", value=f"{format_money(offer.salary)}/season")
    embed.add_field(name="Term", value=f"{offer.term_seasons} season(s)")
    embed.add_field(name="Type", value=offer.contract_type)
    embed.add_field(name="Signing bonus", value=format_money(offer.signing_bonus))
    embed.add_field(name="Kind", value=offer.offer_kind)
    embed.add_field(
        name="Submitted",
        value=discord.utils.format_dt(offer.created_at, style="R"),
    )
    embed.set_footer(
        text="Approving creates the active contract and assigns the team role."
    )
    return embed


def _build_trade_detail(trade: approvals.PendingTrade) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔁 Trade #{trade.trade_id}",
        description=(
            f"**{trade.proposing_team_name}** ⇄ **{trade.other_team_name}**"
        ),
        color=COLOR_WARN,
    )
    embed.add_field(name="Contracts moving", value=str(trade.item_count))
    embed.add_field(
        name="Proposed",
        value=discord.utils.format_dt(trade.created_at, style="R"),
    )
    embed.set_footer(text="Approving transfers the contracts and swaps team roles.")
    return embed


class _QueueSelect(discord.ui.Select):
    def __init__(self, queue: approvals.PendingQueue) -> None:
        options: list[discord.SelectOption] = []
        for o in queue.offers:
            options.append(
                discord.SelectOption(
                    label=f"#{o.offer_id} {o.driver_name} → {o.team_name}"[:100],
                    value=f"{_OFFER}:{o.offer_id}",
                    description=(
                        f"{format_money(o.salary)}/season × {o.term_seasons} "
                        f"· tier {o.tier_code}"
                    )[:100],
                    emoji="📝",
                )
            )
        for t in queue.trades:
            options.append(
                discord.SelectOption(
                    label=(
                        f"#{t.trade_id} {t.proposing_team_name} ⇄ "
                        f"{t.other_team_name}"
                    )[:100],
                    value=f"{_TRADE}:{t.trade_id}",
                    description=f"{t.item_count} contract(s)"[:100],
                    emoji="🔁",
                )
            )

        super().__init__(
            placeholder="Pick an item to review…",
            options=options[:SELECT_MAX_OPTIONS],
            disabled=not options,
        )
        self._queue = queue

    async def callback(self, interaction: discord.Interaction) -> None:
        kind, raw_id = self.values[0].split(":", 1)
        item_id = int(raw_id)
        view = self.view
        assert isinstance(view, ApprovalsView)

        if kind == _OFFER:
            offer = next(
                (o for o in self._queue.offers if o.offer_id == item_id), None
            )
            if offer is None:
                await view.reload(interaction, note="That offer is no longer pending.")
                return
            embed = _build_offer_detail(offer)
        else:
            trade = next(
                (t for t in self._queue.trades if t.trade_id == item_id), None
            )
            if trade is None:
                await view.reload(interaction, note="That trade is no longer pending.")
                return
            embed = _build_trade_detail(trade)

        detail = _ItemView(
            kind=kind,
            item_id=item_id,
            opener_id=view.opener_id,
            parent=view,
        )
        await interaction.response.edit_message(embed=embed, view=detail)


class _RejectModal(discord.ui.Modal):
    """Rejection takes an optional note, which lands in the audit log."""

    def __init__(self, *, kind: str, item_id: int, parent: ApprovalsView) -> None:
        label = "offer" if kind == _OFFER else "trade"
        super().__init__(title=f"Reject {label} #{item_id}")
        self._kind = kind
        self._item_id = item_id
        self._parent = parent
        self._note = discord.ui.TextInput(
            label="Reason (optional)",
            placeholder="Recorded in the audit log and shown to the team.",
            required=False,
            max_length=400,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self._note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        note = self._note.value.strip() or None
        try:
            if self._kind == _OFFER:
                await approvals.reject_offer(
                    offer_id=self._item_id,
                    actor_id=interaction.user.id,
                    note=note,
                )
            else:
                await approvals.reject_trade(
                    trade_id=self._item_id,
                    actor_id=interaction.user.id,
                    note=note,
                )
        except approvals.ApprovalError as exc:
            await report_error(interaction, str(exc))
            return

        label = "Offer" if self._kind == _OFFER else "Trade"
        await self._parent.reload(
            interaction, note=f"✅ {label} #{self._item_id} rejected."
        )


class _ItemView(AdminOwnedView):
    """Approve / Reject for a single queue item."""

    def __init__(
        self, *, kind: str, item_id: int, opener_id: int, parent: ApprovalsView
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.kind = kind
        self.item_id = item_id
        self.parent = parent
        self.add_item(BackButton(self._back, label="Back to queue"))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            if self.kind == _OFFER:
                result = await approvals.approve_offer(
                    interaction.client,
                    guild=interaction.guild,
                    actor=interaction.user,
                    offer_id=self.item_id,
                )
                note = (
                    f"✅ Approved offer #{self.item_id} → contract "
                    f"`{result.contract_id}` (ref `{result.external_ref}`)."
                )
                if result.role_warning:
                    note += f"\n⚠ Role not assigned: {result.role_warning}"
            else:
                trade_result = await approvals.approve_trade(
                    guild=interaction.guild,
                    actor=interaction.user,
                    trade_id=self.item_id,
                )
                note = f"✅ Approved trade #{self.item_id}. Contracts transferred."
                if trade_result.role_warnings:
                    note += "\n⚠ Role swaps had issues:\n" + "\n".join(
                        f"• {w}" for w in trade_result.role_warnings
                    )
        except approvals.ApprovalError as exc:
            await report_error(interaction, str(exc))
            return

        await self.parent.reload(interaction, note=note)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="🚫")
    async def reject(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            _RejectModal(kind=self.kind, item_id=self.item_id, parent=self.parent)
        )


class ApprovalsView(AdminOwnedView):
    """
    The queue screen. Rebuilt from the database after every action so a
    just-approved item cannot be clicked twice.
    """

    def __init__(
        self,
        *,
        queue: approvals.PendingQueue,
        opener_id: int,
        on_back: BackCallback,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self._on_back = on_back
        self.add_item(_QueueSelect(queue))
        self.add_item(BackButton(on_back, row=1))

    async def reload(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        """
        Re-read the queue and re-render.

        Always a fresh read rather than mutating the cached list, because
        an approval can change more than the item acted on — a trade
        approval can resolve offers for the same contract.
        """
        queue = await approvals.fetch_pending_queue(
            interaction.guild_id, limit=_QUEUE_LIMIT
        )
        view = ApprovalsView(
            queue=queue, opener_id=self.opener_id, on_back=self._on_back
        )
        embed = build_approvals_embed(queue)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
            if note:
                await interaction.followup.send(note, ephemeral=True)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
            if note:
                await interaction.followup.send(note, ephemeral=True)


async def open_approvals(
    interaction: discord.Interaction, *, opener_id: int, on_back: BackCallback
) -> None:
    """Entry point used by the panel's Approvals button."""
    queue = await approvals.fetch_pending_queue(
        interaction.guild_id, limit=_QUEUE_LIMIT
    )
    view = ApprovalsView(queue=queue, opener_id=opener_id, on_back=on_back)
    await interaction.response.edit_message(
        embed=build_approvals_embed(queue), view=view
    )
