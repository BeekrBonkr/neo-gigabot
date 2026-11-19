from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from utils.garden import render, rules, store

log = logging.getLogger(__name__)

SESSION_SECONDS = 180          # an untouched panel closes itself after this long
NO_MENTIONS = discord.AllowedMentions.none()
IMAGE_NAME = "greenhouse.png"


def describe(state: dict[str, Any]) -> str:
    """The words that go with the picture."""
    barrel, plant, cost = state["barrel"], state["plant"], state["cost"]
    lines = [f"💧 **{state['water']}** water · ☀️ **{state['sunlight']}** sunlight"]
    if barrel["full"]:
        lines.append(f"🛢️ The rain barrel is full ({barrel['stored']}/{barrel['capacity']}). Collect it so it can keep filling.")
    elif barrel["stored"]:
        lines.append(f"🛢️ The rain barrel holds {barrel['stored']}/{barrel['capacity']} water.")
    else:
        lines.append(f"🛢️ The rain barrel is empty. More water <t:{int(barrel['next_at']) + 1}:R>.")
    if plant is None:
        lines.append("🌱 Your pot is empty. Pick a seed to plant.")
    elif plant["mature"]:
        lines.append(f"🌸 Your **{plant['name']}** is in bloom. Move it to the bench to start a new seed.")
    elif plant["growing"]:
        lines.append(f"🌿 Your **{plant['name']}** ({plant['stage_name'].lower()}) is growing. Ready for more water <t:{int(plant['ready_at']) + 1}:R>.")
    else:
        lines.append(f"🌿 Your **{plant['name']}** ({plant['stage_name'].lower()}) needs water: {cost['water']} water and {cost['sunlight']} sunlight.")
    if state["bench"]:
        lines.append(f"🪴 {state['bench']} grown plant(s) on your bench, waiting for a garden.")
    return "\n".join(lines)


def hint(state: dict[str, Any]) -> str:
    return f"Sunlight comes from chatting in a server I'm in ({state['sun_today']}/{state['sun_daily_cap']} today)."


class GreenhouseView(discord.ui.View):
    """One player's private control panel. Everything they do is remembered,
    and when they finish, one picture of the result is posted for the server."""

    def __init__(self, cog: "Garden", owner: discord.abc.User, state: dict[str, Any], *, public: bool) -> None:
        super().__init__(timeout=SESSION_SECONDS)
        self.cog, self.owner, self.state, self.public = cog, owner, state, public
        self.did: list[str] = []
        self.last: discord.Interaction | None = None
        self.closed = False
        self._build()

    def _build(self) -> None:
        self.clear_items()
        state, can = self.state, self.state["can"]
        if can["plant"]:
            select = discord.ui.Select(
                placeholder="Pick a seed to plant",
                options=[discord.SelectOption(label=s["name"], value=s["species"], emoji="🌱") for s in state["starters"]],
            )
            select.callback = self._plant
            self.add_item(select)
        stored = state["barrel"]["stored"]
        self._button(f"Collect water ({stored})" if stored else "Collect water", "💧", discord.ButtonStyle.primary, not can["collect"], "collect")
        if state["plant"] is not None and not state["plant"]["mature"]:
            self._button("Water plant", "🚿", discord.ButtonStyle.success, not can["water"], "water")
        if can["bench"]:
            self._button("Move to bench", "🪴", discord.ButtonStyle.success, False, "bench")
        self._button("Refresh", "🔄", discord.ButtonStyle.secondary, False, "")
        done = discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary, row=1 if can["plant"] else 0)
        done.callback = self._done
        self.add_item(done)

    def _button(self, label: str, emoji: str, style: discord.ButtonStyle, disabled: bool, action: str) -> None:
        button = discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled, row=1 if self.state["can"]["plant"] else 0)

        async def pressed(interaction: discord.Interaction) -> None:
            await self._do(interaction, action)

        button.callback = pressed
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner.id

    async def _plant(self, interaction: discord.Interaction) -> None:
        await self._do(interaction, "plant", species=interaction.data["values"][0])

    async def _do(self, interaction: discord.Interaction, action: str, *, species: str = "") -> None:
        self.last, note = interaction, ""
        try:
            if action:
                self.state, did = store.act(self.cog.storage, self.owner.id, action, species=species)
                self.did.append(did)
            else:
                self.state = store.get_state(self.cog.storage, self.owner.id)
        except store.GardenError as exc:
            self.state, note = store.get_state(self.cog.storage, self.owner.id), f"⚠️ {exc}"
        self._build()
        embed, file = await self.cog.picture(self.owner, self.state, note=note)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=self)

    async def _done(self, interaction: discord.Interaction) -> None:
        self.last = interaction
        await interaction.response.defer()
        await self._close()

    async def on_timeout(self) -> None:
        await self._close()

    async def _close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stop()
        interaction = self.last
        if interaction is None:
            return
        try:
            self.state = store.get_state(self.cog.storage, self.owner.id)
            embed, file = await self.cog.picture(self.owner, self.state, note="Closed. Open it again with `/greenhouse`.")
            await interaction.edit_original_response(embed=embed, attachments=[file], view=None)
            if self.public and self.did:
                await self.cog.post_summary(interaction, self.owner, self.state, self.did)
        except discord.HTTPException:
            log.info("Could not close a greenhouse panel for %s", self.owner.id, exc_info=True)


def summarize(did: list[str]) -> str:
    """'collected 4 water, watered their Tulip and planted a Tulip seed', with repeats folded."""
    counts: dict[str, int] = {}
    for item in did:
        counts[item] = counts.get(item, 0) + 1
    parts = [item if count == 1 else f"{item} ({count} times)" for item, count in counts.items()]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


class Garden(commands.Cog):
    """The garden game. For now: your own greenhouse, one plant at a time."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.storage = bot.storage_path
        self.players: set[int] = store.player_ids(self.storage)
        self.sunlit: dict[int, float] = {}  # user -> when they last earned sunlight

    async def picture(self, owner: discord.abc.User, state: dict[str, Any], *, note: str = "", title: str | None = None,
                      description: str | None = None, footer: str | None = None) -> tuple[discord.Embed, discord.File]:
        data = await asyncio.to_thread(render.render_greenhouse, state)
        text = description if description is not None else describe(state) + (f"\n\n{note}" if note else "")
        embed = self.bot.embeds.info_embed(title or f"{owner.display_name}'s greenhouse", text)
        embed.set_image(url=f"attachment://{IMAGE_NAME}")
        embed.set_footer(text=footer if footer is not None else hint(state))
        return embed, discord.File(io.BytesIO(data), filename=IMAGE_NAME)

    async def post_summary(self, interaction: discord.Interaction, owner: discord.abc.User, state: dict[str, Any], did: list[str]) -> None:
        """The one public message a visit leaves behind: the greenhouse as it now stands."""
        plant = state["plant"]
        where = "in bloom" if plant and plant["mature"] else f"at the {plant['stage_name'].lower()} stage" if plant else ""
        status = f"\nTheir **{plant['name']}** is {where}." if plant else ""
        embed, file = await self.picture(
            owner, state, title=f"{owner.display_name} tended their greenhouse",
            description=f"{owner.mention} {summarize(did)}.{status}", footer="Grow your own with /greenhouse",
        )
        await interaction.followup.send(embed=embed, file=file, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="greenhouse", description="Tend your own greenhouse: collect water, plant a seed and help it grow.")
    @app_commands.checks.cooldown(2, 10.0)
    async def greenhouse(self, interaction: discord.Interaction) -> None:
        if not render.available():
            await self.bot.embeds.error_interaction(interaction, "Greenhouse Closed", "The greenhouse art is not installed on this bot yet.", ephemeral=True)
            return
        state = store.get_state(self.storage, interaction.user.id)
        self.players.add(interaction.user.id)
        note = "Welcome! The rain barrel is full: collect it, pick a seed, then water it to start it growing." if state["new"] else ""
        view = GreenhouseView(self, interaction.user, state, public=interaction.guild is not None)
        view.last = interaction
        embed, file = await self.picture(interaction.user, state, note=note)
        await interaction.response.send_message(embed=embed, file=file, view=view, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Chatting earns sunlight, for people who have a greenhouse."""
        if message.guild is None or message.author.bot or message.author.id not in self.players:
            return
        now = time.time()
        cooldown = rules.seconds(rules.load()["sunlight"]["cooldown_seconds"])
        if now - self.sunlit.get(message.author.id, 0.0) < cooldown:
            return
        self.sunlit[message.author.id] = now
        store.add_sunlight(self.storage, message.author.id, now=now)


async def setup(bot: commands.Bot) -> None:
    if rules.enabled():
        await bot.add_cog(Garden(bot))
