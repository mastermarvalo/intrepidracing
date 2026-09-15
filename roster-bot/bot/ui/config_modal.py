"""
The league-config modal, shared by `/market-admin config edit` and the
control panel's Setup screen.

It lives outside both cogs so there is exactly one definition. A second
copy would drift the moment someone added a config field to one of them.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import discord

from bot import db, queries


class ConfigModal(discord.ui.Modal):
    """
    5-field modal for the numeric config values. Additional flags
    (channels, roles, free agency) are set via /market-admin config
    channel|role|free-agency. Splitting them this way keeps every input
    inside Discord's 5-input modal limit without cramming toggles into
    text fields.
    """

    def __init__(
        self, *, season_id: int, tier_id: int | None, current
    ) -> None:
        super().__init__(title="Edit league config")
        self._season_id = season_id
        self._tier_id = tier_id
        self._current = current
        self._salary_cap = discord.ui.TextInput(
            label="Salary cap ($M)", default=str(current.salary_cap)
        )
        self._min_salary = discord.ui.TextInput(
            label="Minimum salary ($M)", default=str(current.min_salary)
        )
        self._weekly_cap = discord.ui.TextInput(
            label="Weekly move cap ($M)", default=str(current.weekly_move_cap)
        )
        self._exceptional_cap = discord.ui.TextInput(
            label="Exceptional move cap ($M)",
            default=str(current.exceptional_move_cap),
        )
        self._max_term = discord.ui.TextInput(
            label="Max contract term (seasons)",
            default=str(current.max_term_seasons),
        )
        for widget in (
            self._salary_cap, self._min_salary, self._weekly_cap,
            self._exceptional_cap, self._max_term,
        ):
            self.add_item(widget)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            salary_cap = Decimal(self._salary_cap.value.strip())
            min_salary = Decimal(self._min_salary.value.strip())
            weekly_cap = Decimal(self._weekly_cap.value.strip())
            exceptional_cap = Decimal(self._exceptional_cap.value.strip())
            max_term = int(self._max_term.value.strip())
        except (InvalidOperation, ValueError) as exc:
            await interaction.response.send_message(
                f"Could not parse a number: {exc}", ephemeral=True
            )
            return

        cur = self._current
        async with db.connect() as conn:
            await queries.upsert_league_config(
                conn,
                season_id=self._season_id,
                tier_id=self._tier_id,
                salary_cap=salary_cap,
                min_salary=min_salary,
                max_salary=cur.max_salary,
                active_driver_slots=cur.active_driver_slots,
                weekly_move_cap=weekly_cap,
                exceptional_move_cap=exceptional_cap,
                max_term_seasons=max_term,
                max_incentive_pct=cur.max_incentive_pct,
                offer_ttl_hours=cur.offer_ttl_hours,
            )

        await interaction.response.send_message(
            "✅ League config saved.", ephemeral=True
        )
