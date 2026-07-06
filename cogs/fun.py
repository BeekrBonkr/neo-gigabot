from __future__ import annotations

import asyncio
import html
import random
import time
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote

import aiohttp
import discord
import pyfiglet
from discord import app_commands
from discord.ext import commands

from utils.settings import command_is_blocked, get_guild_settings, is_bot_channel

try:
    import apraw  # type: ignore
except Exception:  # pragma: no cover - handled gracefully at runtime
    apraw = None

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

MEME_SUBREDDITS: Final[list[str]] = [
    "memes",
    "dankmemes",
    "me_irl",
    "wholesomememes",
    "deepfriedmemes",
    "mildlyinfuriating",
]

REDDIT_ICON_URL: Final[str] = "https://www.redditinc.com/assets/images/site/reddit-logo.png"

WIKIPEDIA_SUMMARY_URL: Final[str] = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_SEARCH_URL: Final[str] = (
    "https://en.wikipedia.org/w/api.php?action=opensearch&format=json&limit=5&search={query}"
)
WIKIPEDIA_ICON_URL: Final[str] = (
    "https://upload.wikimedia.org/wikipedia/en/thumb/8/80/Wikipedia-logo-v2.svg/1200px-Wikipedia-logo-v2.svg.png"
)

OPENWEATHERMAP_URL: Final[str] = (
    "https://api.openweathermap.org/data/2.5/weather?q={city}&units=metric&appid={key}"
)
OPENWEATHERMAP_ICON_URL: Final[str] = "https://openweathermap.org/img/wn/{icon}@2x.png"

# Giphy's search API. Results are capped to a PG-13 rating regardless of channel as a
# content-safety default - this is a general-purpose bot, not an NSFW-gated one.
GIPHY_SEARCH_URL: Final[str] = (
    "https://api.giphy.com/v1/gifs/search?api_key={key}&q={query}&limit=15&rating=pg-13&lang=en"
)
CUTE_GIF_QUERY: Final[str] = "cute animals"
BEANS_GIF_QUERY: Final[str] = "beans"

SNIPE_MAX_AGE_SECONDS: Final[int] = 5 * 60  # don't resurrect deleted messages after they go stale
SNIPE_MAX_CONTENT_LENGTH: Final[int] = 1000

HOLDING_TIMES: Final[list[int]] = [15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0]
HOTPOTATO_LOBBY_SECONDS: Final[int] = 30
HOTPOTATO_MIN_PLAYERS: Final[int] = 2


@dataclass(slots=True)
class RedditCredentials:
    client_id: str = ""
    client_secret: str = ""
    user_agent: str = "neo-gigabot/1.0"
    username: str = ""
    password: str = ""

    @property
    def configured(self) -> bool:
        return all(
            [
                self.client_id.strip(),
                self.client_secret.strip(),
                self.user_agent.strip(),
                self.username.strip(),
                self.password.strip(),
            ]
        )


class TriviaView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "Fun",
        author_id: int,
        correct_answer: str,
        options: list[str],
        timeout: float = 30.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.correct_answer = correct_answer
        self.answered = False

        for index, option in enumerate(options):
            self.add_item(TriviaButton(index=index, label=option))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await self.cog.bot.embeds.warning_interaction(
                interaction,
                "Not Your Trivia",
                "Only the person who started this trivia question can answer it.",
                ephemeral=True,
            )
            return False
        return True

    async def finish(self, interaction: discord.Interaction, selected_answer: str | None) -> None:
        if self.answered:
            return

        self.answered = True
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
                if item.label == self.correct_answer:
                    item.style = discord.ButtonStyle.success
                elif selected_answer is not None and item.label == selected_answer:
                    item.style = discord.ButtonStyle.danger

        if interaction.message is not None:
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed is not None:
                if selected_answer is None:
                    embed.add_field(
                        name="Result",
                        value=f"⏰ Time's up. The correct answer was **{self.correct_answer}**.",
                        inline=False,
                    )
                elif selected_answer == self.correct_answer:
                    embed.add_field(
                        name="Result",
                        value=f"✅ Correct. The answer was **{self.correct_answer}**.",
                        inline=False,
                    )
                else:
                    embed.add_field(
                        name="Result",
                        value=(
                            f"❌ Nope. You picked **{selected_answer}**. "
                            f"The correct answer was **{self.correct_answer}**."
                        ),
                        inline=False,
                    )
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        if self.answered:
            return

        self.answered = True
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
                if item.label == self.correct_answer:
                    item.style = discord.ButtonStyle.success

        if hasattr(self, "message") and self.message is not None:
            embed = self.message.embeds[0] if self.message.embeds else None
            if embed is not None:
                embed.add_field(
                    name="Result",
                    value=f"⏰ Time's up. The correct answer was **{self.correct_answer}**.",
                    inline=False,
                )
            await self.message.edit(embed=embed, view=self)


class TriviaButton(discord.ui.Button[TriviaView]):
    def __init__(self, *, index: int, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=index // 2)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.finish(interaction, self.label)


class TicTacToeBoardButton(discord.ui.Button["TicTacToeView"]):
    def __init__(self, position: int) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label="​", row=position // 3)
        self.position = position

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.handle_move(interaction, self.position)


class TicTacToeView(discord.ui.View):
    """Single-player tic-tac-toe against a bot that just picks a random open square."""

    PLAYER_EMOJI = "⭕"
    BOT_EMOJI = "❌"
    WIN_LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))

    def __init__(self, *, author_id: int, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.board: list[str | None] = [None] * 9
        self.message: discord.Message | None = None
        self.finished = False
        for i in range(9):
            self.add_item(TicTacToeBoardButton(i))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return False
        return True

    def _button_for(self, position: int) -> TicTacToeBoardButton:
        return self.children[position]  # type: ignore[return-value]

    def _winner(self) -> str | None:
        for a, b, c in self.WIN_LINES:
            if self.board[a] is not None and self.board[a] == self.board[b] == self.board[c]:
                return self.board[a]
        return None

    def _resolve(self) -> str | None:
        winner = self._winner()
        if winner == self.PLAYER_EMOJI:
            return "player"
        if winner == self.BOT_EMOJI:
            return "bot"
        if all(cell is not None for cell in self.board):
            return "draw"
        return None

    @staticmethod
    def _result_message(result: str) -> str:
        return {
            "player": "🎉 You win!",
            "bot": "🤖 I win! Better luck next time.",
            "draw": "It's a draw!",
        }[result]

    def _end(self) -> None:
        self.finished = True
        for item in self.children:
            item.disabled = True
        self.stop()

    async def handle_move(self, interaction: discord.Interaction, position: int) -> None:
        if self.finished or self.board[position] is not None:
            await interaction.response.defer()
            return

        self.board[position] = self.PLAYER_EMOJI
        button = self._button_for(position)
        button.label = self.PLAYER_EMOJI
        button.style = discord.ButtonStyle.success
        button.disabled = True

        result = self._resolve()
        if result is not None:
            self._end()
            await interaction.response.edit_message(content=self._result_message(result), view=self)
            return

        await interaction.response.edit_message(view=self)
        await asyncio.sleep(1)

        bot_position = random.choice([i for i, cell in enumerate(self.board) if cell is None])
        self.board[bot_position] = self.BOT_EMOJI
        bot_button = self._button_for(bot_position)
        bot_button.label = self.BOT_EMOJI
        bot_button.style = discord.ButtonStyle.danger
        bot_button.disabled = True

        result = self._resolve()
        content = self._result_message(result) if result is not None else None
        if result is not None:
            self._end()

        if self.message is not None:
            try:
                await self.message.edit(content=content, view=self)
            except discord.HTTPException:
                pass

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self._end()
        if self.message is not None:
            try:
                await self.message.edit(content="Tic-tac-toe timed out.", view=self)
            except discord.HTTPException:
                pass


class SimonView(discord.ui.View):
    """Simon Says memory game. The command coroutine drives rounds via play_round()."""

    COLORS: Final[list[str]] = ["🟥", "🟦", "🟨", "🟩"]
    STYLES: Final[list[discord.ButtonStyle]] = [
        discord.ButtonStyle.danger,
        discord.ButtonStyle.primary,
        discord.ButtonStyle.secondary,
        discord.ButtonStyle.success,
    ]
    ROUND_TIMEOUT_SECONDS: Final[float] = 45.0

    def __init__(self, *, author_id: int) -> None:
        # timeout=None: the view itself never auto-expires. Inactivity is handled by
        # play_round()'s own asyncio.wait_for, so there's only one timeout code path.
        super().__init__(timeout=None)
        self.author_id = author_id
        self.message: discord.Message | None = None
        self.pattern: list[int] = []
        self.player_progress: list[int] = []
        self.level = 0
        self.finished = False
        self._round_event = asyncio.Event()
        self._round_result: str | None = None

        for index, (emoji, style) in enumerate(zip(self.COLORS, self.STYLES)):
            button: discord.ui.Button[SimonView] = discord.ui.Button(emoji=emoji, style=style, row=index // 2)
            button.callback = self._make_callback(index)
            self.add_item(button)

        self.set_buttons_enabled(False)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return False
        return True

    def set_buttons_enabled(self, enabled: bool) -> None:
        for item in self.children:
            item.disabled = not enabled

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            if self.finished or self._round_event.is_set():
                return

            step = len(self.player_progress)
            self.player_progress.append(index)

            if index != self.pattern[step]:
                self._round_result = "wrong"
                self._round_event.set()
                return

            if len(self.player_progress) == len(self.pattern):
                self._round_result = "correct"
                self._round_event.set()

        return callback

    async def render(self, *, grid: list[str] | None = None, footer: str) -> None:
        if self.message is None:
            return
        grid = grid or ["⬛"] * 4
        description = f"{grid[0]} | {grid[1]}\n{grid[2]} | {grid[3]}"
        embed = discord.Embed(
            title=f"Simon Says - Level {self.level}",
            description=description,
            color=discord.Color.green(),
        )
        embed.set_footer(text=footer)
        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

    async def _flash(self, position: int) -> None:
        grid = ["⬛"] * 4
        grid[position] = self.COLORS[position]
        await self.render(grid=grid, footer="Watch the pattern!")
        await asyncio.sleep(0.6)
        await self.render(grid=["⬛"] * 4, footer="Watch the pattern!")
        await asyncio.sleep(0.2)

    async def play_round(self) -> str:
        """Show one longer pattern, then wait for input. Returns 'correct', 'wrong', or 'timeout'."""
        self.level += 1
        self.pattern.append(random.randint(0, 3))
        self.player_progress = []
        self._round_result = None
        self._round_event.clear()

        self.set_buttons_enabled(False)
        await self.render(footer="Watch the pattern!")
        for position in self.pattern:
            await self._flash(position)

        self.set_buttons_enabled(True)
        await self.render(footer="Your turn! Repeat the pattern.")

        try:
            await asyncio.wait_for(self._round_event.wait(), timeout=self.ROUND_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self.finished = True
            return "timeout"

        if self._round_result == "wrong":
            self.finished = True
        return self._round_result or "wrong"


@dataclass(slots=True)
class HotPotatoGame:
    channel_id: int
    participants: set[int] = field(default_factory=set)
    holder_id: int | None = None
    round_index: int = 0
    started: bool = False
    pass_event: asyncio.Event = field(default_factory=asyncio.Event)


class HotPotatoJoinView(discord.ui.View):
    def __init__(self, *, game: HotPotatoGame, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.game = game
        self.message: discord.Message | None = None

    @discord.ui.button(label="Join 🥔", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id in self.game.participants:
            await interaction.response.send_message("You're already in!", ephemeral=True)
            return
        self.game.participants.add(interaction.user.id)
        await interaction.response.send_message("You joined the hot potato game! 🥔", ephemeral=True)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Fun(commands.Cog):
    """Slash-command based fun commands ported from the legacy bot."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._reddit: apraw.Reddit | None = None
        self._snipes: dict[int, dict[str, Any]] = {}
        self._hotpotato_games: dict[int, HotPotatoGame] = {}

    async def cog_unload(self) -> None:
        self._hotpotato_games.clear()

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

        settings = get_guild_settings(self.bot.storage_path, interaction.guild.id)
        bot_channels = settings.get("bot_channels", []) or []
        if bot_channels and not is_bot_channel(
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

    async def _fetch_json_with_status(self, url: str) -> tuple[int, dict | list | None]:
        """Like _fetch_json, but also returns the HTTP status so callers can
        distinguish e.g. "not found" from "service misconfigured"."""
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"User-Agent": "neo-gigabot/1.0"}) as response:
                    try:
                        payload = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        payload = None
                    return response.status, payload
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return 0, None

    def _get_reddit_credentials(self) -> RedditCredentials:
        config = getattr(self.bot, "config", None)
        return RedditCredentials(
            client_id=getattr(config, "reddit_client_id", ""),
            client_secret=getattr(config, "reddit_client_secret", ""),
            user_agent=getattr(config, "reddit_user_agent", "neo-gigabot/1.0"),
            username=getattr(config, "reddit_username", ""),
            password=getattr(config, "reddit_password", ""),
        )

    def _get_reddit_client(self) -> apraw.Reddit | None:
        if apraw is None:
            return None

        if self._reddit is not None:
            return self._reddit

        creds = self._get_reddit_credentials()
        if not creds.configured:
            return None

        self._reddit = apraw.Reddit(
            client_id=creds.client_id,
            client_secret=creds.client_secret,
            user_agent=creds.user_agent,
            username=creds.username,
            password=creds.password,
        )
        return self._reddit

    async def _fetch_reddit_submission(self, subreddit_name: str):
        reddit = self._get_reddit_client()
        if reddit is None:
            raise RuntimeError(
                "Reddit is not configured. Check apraw installation and Reddit credentials in your .env/config."
            )

        try:
            subreddit = await reddit.subreddit(subreddit_name)
            submissions = []
            async for post in subreddit.hot(limit=100):
                if not getattr(post, "stickied", False):
                    submissions.append(post)

            if not submissions:
                return None

            return random.choice(submissions)
        except Exception as exc:
            print(f"Reddit fetch failed for r/{subreddit_name}: {exc}")
            raise

    async def _send_reddit_submission(
        self,
        interaction: discord.Interaction,
        *,
        subreddit_name: str,
        submission,
    ) -> None:
        author_name = "[deleted]"
        try:
            author = await submission.author()
            if author is not None:
                author_name = str(author)
        except Exception:
            pass

        description = submission.selftext if getattr(submission, "is_self", False) else ""
        embed = self.bot.embeds.create(
            title=submission.title,
            description=description or None,
            color=discord.Color(0xFF5700),
            author_name=f"/r/{subreddit_name} | u/{author_name}",
            author_icon_url=REDDIT_ICON_URL,
            timestamp=False,
        )
        embed.url = f"https://www.reddit.com{submission.permalink}"

        if getattr(submission, "is_self", False):
            pass
        elif getattr(submission, "url", "").lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            embed.set_image(url=submission.url)
        else:
            media = getattr(submission, "media", None) or {}
            if "reddit_video" in media:
                embed.description = f"[Click here to watch the video]({media['reddit_video'].get('fallback_url', submission.url)})"
            elif getattr(submission, "url", None):
                embed.description = (embed.description + "\n\n" if embed.description else "") + f"[Open post content]({submission.url})"

        embed.add_field(name="Score", value=str(getattr(submission, "score", "?")))
        embed.add_field(name="Comments", value=str(getattr(submission, "num_comments", "?")))
        if getattr(submission, "over_18", False):
            embed.add_field(name="Rating", value="NSFW")

        await self.bot.embeds.respond(interaction, embed=embed)

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

    @app_commands.command(name="server", description="Show information about the current server.")
    @app_commands.checks.cooldown(1, 5.0)
    async def server_info(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "server"):
            return

        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Unavailable",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        channels = guild.channels
        text_channels = sum(isinstance(channel, discord.TextChannel) for channel in channels)
        voice_channels = sum(isinstance(channel, discord.VoiceChannel) for channel in channels)
        categories = sum(isinstance(channel, discord.CategoryChannel) for channel in channels)

        embed = self.bot.embeds.create(
            title=f"Server Information - {guild.name}",
            color=discord.Color(0x8D30E3),
            thumbnail_url=guild.icon.url if guild.icon else None,
            fields=[
                self.bot.embeds.field("Server ID", str(guild.id), inline=True),
                self.bot.embeds.field("Owner", str(guild.owner) if guild.owner else "Unknown", inline=True),
                self.bot.embeds.field("Created On", guild.created_at.strftime("%b %d, %Y"), inline=True),
                self.bot.embeds.field("Members", str(guild.member_count), inline=True),
                self.bot.embeds.field("Roles", str(len(guild.roles)), inline=True),
                self.bot.embeds.field("Channels", str(len(channels)), inline=True),
                self.bot.embeds.field("Text Channels", str(text_channels), inline=True),
                self.bot.embeds.field("Voice Channels", str(voice_channels), inline=True),
                self.bot.embeds.field("Categories", str(categories), inline=True),
                self.bot.embeds.field("Emojis", str(len(guild.emojis)), inline=True),
                self.bot.embeds.field("Boost Level", str(guild.premium_tier), inline=True),
                self.bot.embeds.field("Boosts", str(guild.premium_subscription_count or 0), inline=True),
            ],
        )
        await self.bot.embeds.respond(interaction, embed=embed)

    @app_commands.command(name="reddit", description="Get a random hot post from a subreddit.")
    @app_commands.describe(subreddit="The subreddit name, with or without r/.")
    @app_commands.checks.cooldown(1, 5.0)
    async def reddit_command(
        self,
        interaction: discord.Interaction,
        subreddit: app_commands.Range[str, 1, 100],
    ) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "reddit"):
            return

        if self._get_reddit_client() is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Reddit Unavailable",
                "Reddit is not configured yet. Add the Reddit API credentials to your `.env` first.",
                ephemeral=True,
            )
            return

        subreddit_name = subreddit.strip().replace(" ", "_")
        if subreddit_name.lower().startswith("r/"):
            subreddit_name = subreddit_name[2:]

        await interaction.response.defer(thinking=True)
        try:
            submission = await self._fetch_reddit_submission(subreddit_name)
        except Exception as exc:
            await self.bot.embeds.edit_interaction_response(
                interaction,
                embed=self.bot.embeds.error_embed(
                    "Reddit Failed",
                    f"I couldn't fetch posts from `r/{subreddit_name}`.\n```{exc}```",
                ),
            )
            return

        if submission is None:
            await self.bot.embeds.edit_interaction_response(
                interaction,
                embed=self.bot.embeds.error_embed(
                    "Reddit Failed",
                    f"I couldn't fetch a post from `r/{subreddit_name}` right now.",
                ),
            )
            return

        channel = interaction.channel
        if getattr(submission, "over_18", False) and not getattr(channel, "is_nsfw", lambda: False)():
            await self.bot.embeds.edit_interaction_response(
                interaction,
                embed=self.bot.embeds.warning_embed(
                    "NSFW Post Blocked",
                    "The post I found is marked as NSFW, but this channel is not NSFW.",
                ),
            )
            return

        await interaction.delete_original_response()
        await self._send_reddit_submission(
            interaction,
            subreddit_name=subreddit_name,
            submission=submission,
        )

    @app_commands.command(name="meme", description="Get a meme from Reddit.")
    @app_commands.checks.cooldown(1, 5.0)
    async def meme(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "meme"):
            return

        subreddit_name = random.choice(MEME_SUBREDDITS)
        await self.reddit_command.callback(self, interaction, subreddit_name)

    @app_commands.command(name="cursed", description="Get a cursed image from Reddit.")
    @app_commands.checks.cooldown(1, 5.0)
    async def cursed(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "cursed"):
            return

        if self._get_reddit_client() is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Reddit Unavailable",
                "Reddit is not configured yet. Add the Reddit API credentials to your `.env` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            submission = await self._fetch_reddit_submission("cursedimages")
        except Exception:
            submission = None

        if submission is None:
            await self.bot.embeds.edit_interaction_response(
                interaction,
                embed=self.bot.embeds.error_embed(
                    "Reddit Failed",
                    "I couldn't fetch a cursed image right now.",
                ),
            )
            return

        channel = interaction.channel
        if getattr(submission, "over_18", False) and not getattr(channel, "is_nsfw", lambda: False)():
            await self.bot.embeds.edit_interaction_response(
                interaction,
                embed=self.bot.embeds.warning_embed(
                    "NSFW Post Blocked",
                    "The post I found is marked as NSFW, but this channel is not NSFW.",
                ),
            )
            return

        await interaction.delete_original_response()
        await self._send_reddit_submission(
            interaction,
            subreddit_name="cursedimages",
            submission=submission,
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

        view = TriviaView(
            cog=self,
            author_id=interaction.user.id,
            correct_answer=correct_answer,
            options=choices,
        )
        embed = self.bot.embeds.create(
            title="Trivia Time",
            fields=[
                self.bot.embeds.field("Category", html.unescape(result.get("category", "Unknown")), inline=True),
                self.bot.embeds.field("Difficulty", html.unescape(result.get("difficulty", "unknown")).title(), inline=True),
                self.bot.embeds.field("Question", question, inline=False),
            ],
            footer="Pick an answer below. You have 30 seconds.",
        )
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="wiki", description="Get a Wikipedia summary for a topic.")
    @app_commands.describe(query="What to look up")
    @app_commands.checks.cooldown(1, 5.0)
    async def wiki(self, interaction: discord.Interaction, query: app_commands.Range[str, 1, 200]) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "wiki"):
            return

        await interaction.response.defer(thinking=True)
        title = query.strip().replace(" ", "_")
        data = await self._fetch_json(WIKIPEDIA_SUMMARY_URL.format(title=quote(title)))

        if isinstance(data, dict) and data.get("type") not in {"disambiguation", None} and data.get("extract"):
            extract = data["extract"]
            if len(extract) > 800:
                extract = extract[:797].rstrip() + "..."

            page_url = data.get("content_urls", {}).get("desktop", {}).get("page")
            embed = self.bot.embeds.create(
                title=data.get("title", query),
                description=extract,
                color=discord.Color(0x1A75FF),
                thumbnail_url=(data.get("thumbnail") or {}).get("source"),
                footer="Wikipedia",
            )
            if page_url:
                embed.url = page_url
            await interaction.followup.send(embed=embed)
            return

        # Exact title didn't resolve to a usable summary (missing, or a disambiguation
        # page) - offer close matches instead of just failing.
        search_results = await self._fetch_json(WIKIPEDIA_SEARCH_URL.format(query=quote(query)))
        suggestions = search_results[1] if isinstance(search_results, list) and len(search_results) > 1 else []

        if suggestions:
            options = "\n".join(f"- {title}" for title in suggestions[:5])
            await interaction.followup.send(
                embed=self.bot.embeds.warning_embed(
                    "Did You Mean?",
                    f"I couldn't find an exact match for `{query}`. Close matches:\n{options}",
                )
            )
        else:
            await interaction.followup.send(
                embed=self.bot.embeds.error_embed("Not Found", f"I couldn't find anything on Wikipedia for `{query}`.")
            )

    @app_commands.command(name="weather", description="Get the current weather for a city.")
    @app_commands.describe(city="City name, optionally 'city,country-code'")
    @app_commands.checks.cooldown(1, 5.0)
    async def weather(self, interaction: discord.Interaction, city: app_commands.Range[str, 1, 100]) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "weather"):
            return

        api_key = getattr(self.bot.config, "openweathermap_api_key", "")
        if not api_key:
            await self.bot.embeds.error_interaction(
                interaction,
                "Weather Unavailable",
                "Weather isn't configured yet. Add `OPENWEATHERMAP_API_KEY` to `.env` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        url = OPENWEATHERMAP_URL.format(city=quote(city), key=api_key)
        status, data = await self._fetch_json_with_status(url)

        if status == 404:
            await interaction.followup.send(
                embed=self.bot.embeds.error_embed("City Not Found", f"I couldn't find a city called `{city}`.")
            )
            return
        if status == 401:
            self.bot.logger.warning("OpenWeatherMap rejected the configured API key (401).")
            await interaction.followup.send(
                embed=self.bot.embeds.error_embed("Weather Unavailable", "The weather service is misconfigured. Ask the bot owner to check the API key.")
            )
            return
        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send(
                embed=self.bot.embeds.error_embed("Weather Failed", "I couldn't fetch the weather right now.")
            )
            return

        weather_info = (data.get("weather") or [{}])[0]
        main = data.get("main", {})
        wind = data.get("wind", {})
        celsius = main.get("temp")
        fahrenheit = celsius * 9 / 5 + 32 if isinstance(celsius, (int, float)) else None
        feels_like_c = main.get("feels_like")

        resolved_name = data.get("name") or city
        country = (data.get("sys") or {}).get("country")
        location = f"{resolved_name}, {country}" if country else resolved_name

        embed = self.bot.embeds.create(
            title=f"Weather in {location}",
            description=str(weather_info.get("description", "Unknown")).title(),
            color=discord.Color(0x1E90FF),
            thumbnail_url=OPENWEATHERMAP_ICON_URL.format(icon=weather_info["icon"]) if weather_info.get("icon") else None,
            fields=[
                self.bot.embeds.field(
                    "Temperature",
                    f"{celsius:.1f}°C / {fahrenheit:.1f}°F" if fahrenheit is not None else "Unknown",
                    inline=True,
                ),
                self.bot.embeds.field(
                    "Feels Like", f"{feels_like_c:.1f}°C" if isinstance(feels_like_c, (int, float)) else "Unknown", inline=True
                ),
                self.bot.embeds.field("Humidity", f"{main.get('humidity', '?')}%", inline=True),
                self.bot.embeds.field("Wind Speed", f"{wind.get('speed', '?')} m/s", inline=True),
            ],
            footer="Weather data from OpenWeatherMap",
        )
        await interaction.followup.send(embed=embed)

    async def _send_giphy_result(self, interaction: discord.Interaction, query: str, *, title: str) -> None:
        api_key = getattr(self.bot.config, "giphy_api_key", "")
        if not api_key:
            await self.bot.embeds.error_interaction(
                interaction,
                "GIFs Unavailable",
                "GIF search isn't configured yet. Add `GIPHY_API_KEY` to `.env` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        url = GIPHY_SEARCH_URL.format(key=api_key, query=quote(query))
        data = await self._fetch_json(url)
        results = data.get("data") if isinstance(data, dict) else None

        if not results:
            await interaction.followup.send(
                embed=self.bot.embeds.error_embed("No Results", f"I couldn't find a GIF for `{query}`.")
            )
            return

        choice = random.choice(results)
        image_url = (((choice.get("images") or {}).get("original") or {}).get("url"))
        if not image_url:
            await interaction.followup.send(embed=self.bot.embeds.error_embed("No Results", "That GIF couldn't be loaded."))
            return

        embed = self.bot.embeds.create(title=title, color=discord.Color(0x00FF99), image_url=image_url, footer="Powered by GIPHY")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="gif", description="Search for a GIF.")
    @app_commands.describe(query="What kind of GIF to search for")
    @app_commands.checks.cooldown(1, 5.0)
    async def gif(self, interaction: discord.Interaction, query: app_commands.Range[str, 1, 100]) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "gif"):
            return
        await self._send_giphy_result(interaction, query, title=f"GIF: {query}")

    @app_commands.command(name="cute", description="Get a random cute animal GIF.")
    @app_commands.checks.cooldown(1, 5.0)
    async def cute(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "cute"):
            return
        await self._send_giphy_result(interaction, CUTE_GIF_QUERY, title="Something Cute")

    @app_commands.command(name="beans", description="Get a random beans GIF.")
    @app_commands.checks.cooldown(1, 5.0)
    async def beans(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "beans"):
            return
        await self._send_giphy_result(interaction, BEANS_GIF_QUERY, title="Beans")

    @app_commands.command(name="snipe", description="Show the most recently deleted message in this channel.")
    @app_commands.checks.cooldown(1, 5.0)
    @app_commands.guild_only()
    async def snipe(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "snipe"):
            return

        snipe = self._snipes.get(interaction.channel_id)
        if snipe is None or (time.time() - snipe["timestamp"]) > SNIPE_MAX_AGE_SECONDS:
            await self.bot.embeds.info_interaction(interaction, "Nothing To Snipe", "No recently deleted message found in this channel.")
            return

        content = snipe["content"] or "*(no text content - the message may have only had an attachment)*"
        embed = self.bot.embeds.create(
            description=content,
            color=discord.Color(0xFF5733),
            author_name=snipe["author_name"],
            author_icon_url=snipe["author_avatar_url"],
            footer=f"Sniped by {interaction.user}",
            timestamp=False,
        )
        if snipe["attachment_count"]:
            embed.add_field(name="Attachments", value=str(snipe["attachment_count"]), inline=True)
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        content = (message.content or "")[:SNIPE_MAX_CONTENT_LENGTH]
        self._snipes[message.channel.id] = {
            "content": content,
            "author_name": str(message.author),
            "author_avatar_url": message.author.display_avatar.url,
            "attachment_count": len(message.attachments),
            "timestamp": time.time(),
        }

    @app_commands.command(name="tictactoe", description="Play tic-tac-toe against the bot.")
    @app_commands.checks.cooldown(1, 10.0)
    async def tictactoe(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "tictactoe"):
            return

        view = TicTacToeView(author_id=interaction.user.id)
        embed = discord.Embed(title="Tic-Tac-Toe", description="Your move! You're ⭕, I'm ❌.", color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="simon", description="Play a Simon Says memory game.")
    @app_commands.checks.cooldown(1, 10.0)
    async def simon(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "simon"):
            return

        view = SimonView(author_id=interaction.user.id)
        embed = discord.Embed(
            title="Simon Says - Get Ready",
            description="⬛ | ⬛\n⬛ | ⬛",
            color=discord.Color.green(),
        )
        embed.set_footer(text="Watch closely...")
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()
        await asyncio.sleep(1.5)

        result = "wrong"
        while True:
            result = await view.play_round()
            if result != "correct":
                break
            await view.render(footer=f"✅ Correct! Moving to level {view.level + 1}...")
            await asyncio.sleep(1.5)

        view.set_buttons_enabled(False)
        view.stop()
        if result == "timeout":
            final = f"⏰ {interaction.user.mention}, you took too long! You reached level {view.level}."
        else:
            final = f"❌ Wrong pattern, {interaction.user.mention}! You reached level {view.level}."
        if view.message is not None:
            try:
                await view.message.edit(content=final, view=view)
            except discord.HTTPException:
                pass

    @app_commands.command(name="hotpotato", description="Start a hot potato game - pass it before time runs out!")
    @app_commands.checks.cooldown(1, 10.0)
    @app_commands.guild_only()
    async def hotpotato(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_fun_command_allowed(interaction, "hotpotato"):
            return

        channel_id = interaction.channel_id
        if channel_id in self._hotpotato_games:
            await self.bot.embeds.warning_interaction(
                interaction, "Game In Progress", "A hot potato game is already running in this channel.", ephemeral=True
            )
            return

        game = HotPotatoGame(channel_id=channel_id)
        self._hotpotato_games[channel_id] = game

        try:
            deadline = int(time.time()) + HOTPOTATO_LOBBY_SECONDS
            view = HotPotatoJoinView(game=game, timeout=HOTPOTATO_LOBBY_SECONDS + 5)
            embed = self.bot.embeds.info_embed(
                "🥔 Hot Potato",
                f"Click **Join** to play! Starting <t:{deadline}:R>. Need at least {HOTPOTATO_MIN_PLAYERS} players.",
            )
            await interaction.response.send_message(embed=embed, view=view)
            view.message = await interaction.original_response()

            await asyncio.sleep(HOTPOTATO_LOBBY_SECONDS)
            view.stop()
            for item in view.children:
                item.disabled = True
            try:
                await view.message.edit(view=view)
            except discord.HTTPException:
                pass

            channel = interaction.channel
            if len(game.participants) < HOTPOTATO_MIN_PLAYERS:
                await interaction.followup.send(
                    embed=self.bot.embeds.warning_embed(
                        "Not Enough Players", f"At least {HOTPOTATO_MIN_PLAYERS} players are needed. Maybe next time!"
                    )
                )
                return

            game.started = True
            game.holder_id = random.choice(list(game.participants))
            await channel.send(
                embed=self.bot.embeds.info_embed(
                    "🥔 The Game Begins!",
                    f"<@{game.holder_id}> has the potato! Mention another player in this channel to pass it "
                    f"before **{HOLDING_TIMES[0]}** seconds run out!",
                )
            )

            while game.round_index < len(HOLDING_TIMES):
                holding_time = HOLDING_TIMES[game.round_index]
                game.pass_event.clear()
                try:
                    await asyncio.wait_for(game.pass_event.wait(), timeout=max(holding_time, 0.05))
                except asyncio.TimeoutError:
                    await channel.send(
                        embed=self.bot.embeds.error_embed(
                            "💥 Boom!", f"<@{game.holder_id}> was holding the potato too long and got burned!"
                        )
                    )
                    return

            await channel.send(
                embed=self.bot.embeds.error_embed(
                    "💥 Boom!",
                    f"<@{game.holder_id}> made it through every round and still got caught holding the potato!",
                )
            )
        finally:
            self._hotpotato_games.pop(channel_id, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        game = self._hotpotato_games.get(message.channel.id)
        if game is None or not game.started or message.author.id != game.holder_id or not message.mentions:
            return

        for mentioned in message.mentions:
            if mentioned.bot or mentioned.id == game.holder_id or mentioned.id not in game.participants:
                continue

            game.holder_id = mentioned.id
            game.round_index += 1
            game.pass_event.set()

            if game.round_index < len(HOLDING_TIMES):
                remaining = HOLDING_TIMES[game.round_index]
                await message.channel.send(
                    f"🥔 {mentioned.mention}, you have the potato! You have **{remaining}** second(s) to pass it on!"
                )
            return

    @ping.error
    @coinflip.error
    @roll.error
    @ascii_text.error
    @eight_ball.error
    @catfact.error
    @fact.error
    @joke.error
    @server_info.error
    @reddit_command.error
    @meme.error
    @cursed.error
    @insult.error
    @hug.error
    @kiss.error
    @kill.error
    @trivia.error
    @wiki.error
    @weather.error
    @gif.error
    @cute.error
    @beans.error
    @snipe.error
    @tictactoe.error
    @simon.error
    @hotpotato.error
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
