from __future__ import annotations

import html
import random
from dataclasses import dataclass
from typing import Final

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


class Fun(commands.Cog):
    """Slash-command based fun commands ported from the legacy bot."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._reddit: apraw.Reddit | None = None

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
