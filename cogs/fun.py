from __future__ import annotations

import html
import random
from typing import Final

import aiohttp
import discord
import pyfiglet
from discord import app_commands
from discord.ext import commands

from utils.settings import command_is_blocked, is_bot_channel

EIGHT_BALL_RESPONSES: Final[list[str]] = [
    "It is certain.",
    "It is decidedly so.",
    "Without a doubt.",
    "Yes, definitely.",
    "You may rely on it.",
    "As I see it, yes.",
    "Most likely.",
    "Outlook good.",
    "Yes.",
    "Signs point to yes.",
    "Reply hazy, try again.",
    "Ask again later.",
    "Better not tell you now.",
    "Cannot predict now.",
    "Concentrate and ask again.",
    "Don't count on it.",
    "My reply is no.",
    "My sources say no.",
    "Outlook not so good.",
    "Very doubtful.",
]

INSULTS: Final[list[str]] = [
    "is built like a wet napkin.",
    "has the energy of a dying toaster.",
    "could lose an argument with a brick.",
    "is proof that auto-correct has limits.",
    "has the tactical awareness of a potato.",
    "is somehow both loud and wrong.",
    "would trip over a cordless phone.",
    "has the charisma of unseasoned chicken.",
    "makes dial-up internet look efficient.",
    "is running on 2 brain cells and both are on break.",
]

HUG_MESSAGES: Final[list[str]] = [
    "{author} gives {target} a huge hug. 🤗",
    "{author} wraps {target} in a cozy hug. 🫂",
    "{author} tackles {target} with affection. 🤗",
]

KISS_MESSAGES: Final[list[str]] = [
    "{author} kisses {target}. 😳",
    "{author} gives {target} a dramatic kiss. 💋",
    "{author} sneaks a quick kiss to {target}. 😘",
]

KILL_MESSAGES: Final[list[str]] = [
    "{author} bonks {target} out of existence. 💥",
    "{author} defeats {target} in an extremely fair battle. ⚔️",
    "{author} sends {target} to the shadow realm. ☠️",
]


class Fun(commands.Cog):
    """Slash-command based fun commands ported from the legacy bot."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _ensure_fun_command_allowed(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> bool:
        if interaction.guild is None or interaction.channel is None:
            return True

        if command_is_blocked(self.bot.storage_path, interaction.guild.id, command_name):
            await self.bot.embeds.error_interaction(
                interaction,
                "Command Blocked",
                f"`/{command_name}` is blocked in this server.",
                ephemeral=True,
            )
            return False

        if not is_bot_channel(
            self.bot.storage_path,
            interaction.guild.id,
            interaction.channel.id,
        ):
            await self.bot.embeds.warning_interaction(
                interaction,
                "Wrong Channel",
                "This command can only be used in a configured bot channel.",
                ephemeral=True,
            )
            return False

        return True

    async def _fetch_json(self, url: str) -> dict | list | None:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                headers={"User-Agent": "neo-gigabot/1.0"},
            ) as response:
                if response.status != 200:
                    return None
                return await response.json()

    async def _send_action_response(
        self,
        interaction: discord.Interaction,
        command_name: str,
        user: discord.Member | discord.User,
        templates: list[str],
        title: str,
    ) -> None:
        if not await self._ensure_fun_command_allowed(interaction, command_name):
            return

        if interaction.user.id == user.id:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Nice Try",
                f"You can't use `/{command_name}` on yourself.",
                ephemeral=True,
            )
            return

        description = random.choice(templates).format(
            author=interaction.user.mention,
            target=user.mention,
        )
        await self.bot.embeds.info_interaction(interaction, title, description)

    @app_commands.command(name="ping", description="Check the bot's gateway latency.")
    @app_commands.checks.cooldown(1, 5.0)
    async def ping(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "ping"):
            return

        latency_ms = round(self.bot.latency * 1000)
        await self.bot.embeds.info_interaction(
            interaction,
            "Pong",
            f"Gateway latency: `{latency_ms}ms`",
        )

    @app_commands.command(name="coinflip", description="Flip a coin.")
    @app_commands.checks.cooldown(1, 5.0)
    async def coinflip(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "coinflip"):
            return

        result = random.choice(["Heads", "Tails"])
        await self.bot.embeds.info_interaction(
            interaction,
            "Coin Flip",
            f"It landed on **{result}**. 🪙",
        )

    @app_commands.command(name="roll", description="Roll a die with a custom number of sides.")
    @app_commands.describe(size="How many sides the die should have.")
    @app_commands.checks.cooldown(1, 5.0)
    async def roll(self, interaction: discord.Interaction, size: app_commands.Range[int, 1, 1000] = 6) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "roll"):
            return

        number = random.randint(1, size)
        await self.bot.embeds.info_interaction(
            interaction,
            "Dice Roll",
            f"You rolled a **{number}** on a **{size}**-sided die. 🎲",
        )

    @app_commands.command(name="ascii", description="Turn short text into ASCII art.")
    @app_commands.describe(text="Text to convert. Keep it short for readable output.")
    @app_commands.checks.cooldown(1, 5.0)
    async def ascii_text(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 15],
    ) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "ascii"):
            return

        ascii_art = pyfiglet.figlet_format(text)
        if len(ascii_art) > 1900:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Too Large",
                "That text expands into too much ASCII art. Try something shorter.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(f"```text\n{ascii_art}\n```")

    @app_commands.command(name="8ball", description="Ask the magic 8-ball a yes or no question.")
    @app_commands.describe(question="Your yes or no question.")
    @app_commands.checks.cooldown(1, 3.0)
    async def eight_ball(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, 250],
    ) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "8ball"):
            return

        response = random.choice(EIGHT_BALL_RESPONSES)
        fields = [
            self.bot.embeds.field("Question", question, inline=False),
            self.bot.embeds.field("Answer", f"🎱 {response}", inline=False),
        ]
        await self.bot.embeds.respond(
            interaction,
            title="Magic 8-Ball",
            fields=fields,
        )

    @app_commands.command(name="catfact", description="Get a random cat fact.")
    @app_commands.checks.cooldown(1, 5.0)
    async def catfact(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "catfact"):
            return

        data = await self._fetch_json("https://catfact.ninja/fact")
        fact = data.get("fact") if isinstance(data, dict) else None
        if not fact:
            await self.bot.embeds.error_interaction(
                interaction,
                "Cat Fact Failed",
                "I couldn't fetch a cat fact right now.",
                ephemeral=True,
            )
            return

        await self.bot.embeds.info_interaction(
            interaction,
            "Cat Fact",
            f"🐱 {fact}",
        )

    @app_commands.command(name="fact", description="Get a random useless fact.")
    @app_commands.checks.cooldown(1, 5.0)
    async def fact(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "fact"):
            return

        data = await self._fetch_json("https://uselessfacts.jsph.pl/api/v2/facts/random?language=en")
        fact = data.get("text") if isinstance(data, dict) else None
        if not fact:
            await self.bot.embeds.error_interaction(
                interaction,
                "Fact Failed",
                "I couldn't fetch a fact right now.",
                ephemeral=True,
            )
            return

        await self.bot.embeds.info_interaction(
            interaction,
            "Random Fact",
            fact,
        )

    @app_commands.command(name="joke", description="Get a random joke.")
    @app_commands.checks.cooldown(1, 5.0)
    async def joke(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "joke"):
            return

        data = await self._fetch_json("https://official-joke-api.appspot.com/random_joke")
        if not isinstance(data, dict):
            await self.bot.embeds.error_interaction(
                interaction,
                "Joke Failed",
                "I couldn't fetch a joke right now.",
                ephemeral=True,
            )
            return

        setup = data.get("setup")
        punchline = data.get("punchline")
        if not setup or not punchline:
            await self.bot.embeds.error_interaction(
                interaction,
                "Joke Failed",
                "I couldn't fetch a joke right now.",
                ephemeral=True,
            )
            return

        await self.bot.embeds.respond(
            interaction,
            title="Joke",
            fields=[
                self.bot.embeds.field("Setup", setup, inline=False),
                self.bot.embeds.field("Punchline", punchline, inline=False),
            ],
        )

    @app_commands.command(name="insult", description="Lightly roast someone.")
    @app_commands.describe(user="The user to roast. Leave empty to roast yourself.")
    @app_commands.checks.cooldown(1, 5.0)
    async def insult(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User | None = None,
    ) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "insult"):
            return

        target = user or interaction.user
        description = f"{target.mention} {random.choice(INSULTS)}"
        await self.bot.embeds.warning_interaction(
            interaction,
            "Roast Delivered",
            description,
        )

    @app_commands.command(name="hug", description="Give someone a hug.")
    @app_commands.describe(user="Who should get the hug?")
    @app_commands.checks.cooldown(1, 5.0)
    async def hug(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User,
    ) -> None:
        await self._send_action_response(interaction, "hug", user, HUG_MESSAGES, "Hug")

    @app_commands.command(name="kiss", description="Kiss someone.")
    @app_commands.describe(user="Who should get the kiss?")
    @app_commands.checks.cooldown(1, 5.0)
    async def kiss(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User,
    ) -> None:
        await self._send_action_response(interaction, "kiss", user, KISS_MESSAGES, "Kiss")

    @app_commands.command(name="kill", description="Fictionally defeat someone.")
    @app_commands.describe(user="Who should be dramatically defeated?")
    @app_commands.checks.cooldown(1, 5.0)
    async def kill(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User,
    ) -> None:
        await self._send_action_response(interaction, "kill", user, KILL_MESSAGES, "Totally Fictional Violence")

    @app_commands.command(name="trivia", description="Get a multiple-choice trivia question.")
    @app_commands.checks.cooldown(1, 10.0)
    async def trivia(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "trivia"):
            return

        data = await self._fetch_json("https://opentdb.com/api.php?amount=1&type=multiple")
        if not isinstance(data, dict) or not data.get("results"):
            await self.bot.embeds.error_interaction(
                interaction,
                "Trivia Failed",
                "I couldn't fetch a trivia question right now.",
                ephemeral=True,
            )
            return

        result = data["results"][0]
        question = html.unescape(result["question"])
        correct_answer = html.unescape(result["correct_answer"])
        incorrect_answers = [html.unescape(choice) for choice in result["incorrect_answers"]]
        choices = incorrect_answers + [correct_answer]
        random.shuffle(choices)

        choice_lines = []
        for index, choice in enumerate(choices, start=1):
            marker = "✅" if choice == correct_answer else "•"
            choice_lines.append(f"{index}. {choice} {marker}")

        await self.bot.embeds.respond(
            interaction,
            title="Trivia Time",
            fields=[
                self.bot.embeds.field("Question", question, inline=False),
                self.bot.embeds.field("Choices", "\n".join(choice_lines), inline=False),
                self.bot.embeds.field("Answer", correct_answer, inline=False),
            ],
            footer="This version reveals the answer immediately. We can make it interactive next.",
        )

    @ping.error
    @coinflip.error
    @roll.error
    @ascii_text.error
    @eight_ball.error
    @catfact.error
    @fact.error
    @joke.error
    @insult.error
    @hug.error
    @kiss.error
    @kill.error
    @trivia.error
    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await self.bot.embeds.warning_interaction(
                interaction,
                "Slow Down",
                f"Try again in `{error.retry_after:.1f}` seconds.",
                ephemeral=True,
            )
            return

        if isinstance(error, app_commands.TransformerError):
            await self.bot.embeds.error_interaction(
                interaction,
                "Invalid Value",
                "One of the values you entered was invalid.",
                ephemeral=True,
            )
            return

        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun(bot))
