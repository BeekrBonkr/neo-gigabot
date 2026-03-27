from __future__ import annotations

from typing import Optional

import discord
from discord.ext import commands

from utils.settings import (
    ALLOWED_PREFIX_CHARS,
    create_guild_settings,
    delete_guild_settings,
    get_default_settings,
    get_guild_settings,
    guild_settings_exists,
    save_guild_settings,
    update_guild_settings,
)


class Settings(commands.Cog):
    """Per-guild settings and configuration commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ===== listeners =====
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        create_guild_settings(self.bot.storage_path, guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or self.bot.user is None:
            return

        mention_variants = {f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"}
        if message.content.strip() in mention_variants:
            settings = get_guild_settings(self.bot.storage_path, message.guild.id)
            prefix = settings.get("prefix", self.bot.config.default_prefix)
            await message.channel.send(
                f"You called? My prefix for this server is `{prefix}`. "
                f"Admins can change it by using `{prefix}prefix <new prefix>`"
            )

    # ===== commands =====
    @commands.command(usage="on/off #channel")
    @commands.has_permissions(administrator=True)
    async def suggestion(
        self,
        ctx: commands.Context,
        option: str,
        *channels: discord.TextChannel,
    ) -> None:
        """Set or unset the suggestion channel."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        server_data = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        option = option.lower()

        if option == "on":
            if not channels:
                await ctx.send("Missing channel(s) to set as suggestion channel(s).")
                return
            channel_ids = [str(channel.id) for channel in channels]
            server_data["suggestion_channel_ids"] = channel_ids
            save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
            await ctx.send(
                f"The suggestion channel(s) have been set to {', '.join(channel.mention for channel in channels)}."
            )
            return

        if option == "off":
            if server_data.get("suggestion_channel_ids"):
                server_data["suggestion_channel_ids"] = []
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await ctx.send("The suggestion channel(s) have been unset.")
            else:
                await ctx.send("There are no suggestion channels to unset.")
            return

        await ctx.send("Invalid option. Please use `on` or `off`.")

    @commands.command(usage="<prefix>")
    @commands.has_permissions(administrator=True)
    async def prefix(self, ctx: commands.Context, new_prefix: str) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        if not new_prefix or not all(c in ALLOWED_PREFIX_CHARS for c in new_prefix):
            await ctx.send(
                f"Invalid prefix. Only the following characters are allowed: {ALLOWED_PREFIX_CHARS}"
            )
            return

        update_guild_settings(self.bot.storage_path, ctx.guild.id, {"prefix": new_prefix})
        await ctx.send(f"Prefix updated to `{new_prefix}`")

    @commands.command(name="command", usage="block/allow <command>")
    @commands.has_permissions(administrator=True)
    async def command_permission(
        self,
        ctx: commands.Context,
        permission_type: str,
        command_name: str,
    ) -> None:
        """Allow or block a command in this server."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        server_data = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        blocked_commands = server_data.get("blocked_commands", []) or []

        if command_name.startswith("!"):
            await ctx.send('Error: Command name cannot start with "!" character.')
            return

        permission_type = permission_type.lower()
        if permission_type == "block":
            if command_name not in blocked_commands:
                blocked_commands.append(command_name)
                server_data["blocked_commands"] = blocked_commands
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await ctx.send(f"The `{command_name}` command has been blocked.")
            else:
                await ctx.send(f"The `{command_name}` command is already blocked.")
            return

        if permission_type == "allow":
            if command_name in blocked_commands:
                blocked_commands.remove(command_name)
                server_data["blocked_commands"] = blocked_commands
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await ctx.send(f"The `{command_name}` command has been allowed.")
            else:
                await ctx.send(f"The `{command_name}` command is not blocked.")
            return

        await ctx.send('Error: Invalid permission type. Please use "block" or "allow".')

    @commands.command(name="botchannel", usage="add/remove #channel")
    @commands.has_permissions(administrator=True)
    async def manage_bot_channel(
        self,
        ctx: commands.Context,
        action: str,
        channel: discord.TextChannel,
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        server_data = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        bot_channels = server_data.get("bot_channels", []) or []
        action = action.lower()

        if action == "add":
            if channel.id not in bot_channels:
                bot_channels.append(channel.id)
                server_data["bot_channels"] = bot_channels
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await ctx.send(f"Added {channel.mention} to the bot channels list.")
            else:
                await ctx.send(f"{channel.mention} is already a bot channel.")
            return

        if action == "remove":
            if channel.id in bot_channels:
                bot_channels.remove(channel.id)
                server_data["bot_channels"] = bot_channels
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await ctx.send(f"Removed {channel.mention} from the bot channels list.")
            else:
                await ctx.send(f"{channel.mention} is not currently a bot channel.")
            return

        await ctx.send("Invalid action. Use `add` or `remove`.")

    @commands.command(name="botlisten")
    @commands.has_permissions(administrator=True)
    async def botlisten(self, ctx: commands.Context, status: str) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        status = status.lower()
        if status not in ["on", "off"]:
            await ctx.send("Error: Please enter either 'on' or 'off'.")
            return

        update_guild_settings(
            self.bot.storage_path,
            ctx.guild.id,
            {"listen_to_bots": status == "on"},
        )
        await ctx.send(f"Bot listening status has been set to {status}.")

    @commands.command(name="setup")
    @commands.has_permissions(administrator=True)
    async def create_yaml_file(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        if guild_settings_exists(self.bot.storage_path, ctx.guild.id):
            await ctx.send("This server already has a configuration file.")
            return

        create_guild_settings(self.bot.storage_path, ctx.guild.id)
        await ctx.send(f"Created YAML file for server {ctx.guild.id}.")

    @commands.command(name="resetyaml")
    @commands.has_permissions(administrator=True)
    async def reset_yaml_file(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        def check(reaction: discord.Reaction, user: discord.User | discord.Member) -> bool:
            return (
                user == ctx.author
                and reaction.message.id == confirmation_msg.id
                and str(reaction.emoji) in ["✅", "❎"]
            )

        confirmation_msg = await ctx.send("Are you sure you want to reset the server settings?")
        await confirmation_msg.add_reaction("✅")
        await confirmation_msg.add_reaction("❎")

        try:
            reaction, _ = await self.bot.wait_for("reaction_add", timeout=20.0, check=check)
        except TimeoutError:
            await ctx.send("Timeout: Cancelled.")
            return

        if str(reaction.emoji) == "❎":
            await ctx.send("Cancelled.")
            return

        delete_guild_settings(self.bot.storage_path, ctx.guild.id)
        create_guild_settings(self.bot.storage_path, ctx.guild.id)
        await ctx.send(f"Reset YAML file for server {ctx.guild.id}.")

    @commands.command(name="settings")
    @commands.has_permissions(administrator=True)
    async def show_config(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        server_data = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        default_data = get_default_settings(self.bot.storage_path)
        ordered_keys = list(default_data.keys())

        embed = discord.Embed(title=f"Settings for {ctx.guild.name}", color=discord.Color.blurple())
        if ctx.guild.icon:
            embed.set_author(name=ctx.guild.name, icon_url=ctx.guild.icon.url)
        else:
            embed.set_author(name=ctx.guild.name)

        sorted_server_data = {key: server_data[key] for key in ordered_keys if key in server_data}
        for key, value in sorted_server_data.items():
            rendered = self._render_setting_value(key, value)
            embed.add_field(name=key, value=rendered, inline=False)

        await ctx.send(embed=embed)

    @commands.command(usage="on/off #channel")
    @commands.has_permissions(administrator=True)
    async def log(
        self,
        ctx: commands.Context,
        option: str,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        server_data = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        option = option.lower()

        if option == "on":
            if channel is None:
                await ctx.send("Please specify a channel to log to.")
                return
            server_data["logging_enabled"] = True
            server_data["logging_channel_id"] = str(channel.id)
            save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
            await ctx.send(f"Logging enabled for channel {channel.mention}!")
            return

        if option == "off":
            server_data["logging_enabled"] = False
            server_data["logging_channel_id"] = ""
            save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
            await ctx.send("Logging has been disabled.")
            return

        await ctx.send("Invalid option. Use `on` or `off`.")

    @commands.command(usage="ignore/listen #channel")
    @commands.has_permissions(administrator=True)
    async def logchannel(
        self,
        ctx: commands.Context,
        option: str,
        channel: discord.TextChannel,
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        server_data = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        ignored_channels = server_data.get("ignored_log_channels", []) or []
        option = option.lower()

        if option == "ignore":
            if channel.id not in ignored_channels:
                ignored_channels.append(channel.id)
                server_data["ignored_log_channels"] = ignored_channels
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await ctx.send(f"{channel.mention} has been added to the ignored log channels.")
            else:
                await ctx.send(f"{channel.mention} is already in the ignored log channels.")
            return

        if option == "listen":
            if channel.id in ignored_channels:
                ignored_channels.remove(channel.id)
                server_data["ignored_log_channels"] = ignored_channels
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await ctx.send(f"{channel.mention} has been removed from the ignored log channels.")
            else:
                await ctx.send(f"{channel.mention} is not in the ignored log channels.")
            return

        await ctx.send(f"Invalid option: {option}. Available options are `ignore` and `listen`.")

    @commands.command(usage="ignore/listen @user")
    @commands.has_permissions(administrator=True)
    async def userlog(
        self,
        ctx: commands.Context,
        option: str,
        user: discord.Member,
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        server_data = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        ignored_users = server_data.get("ignored_users", []) or []
        option = option.lower()

        if option == "ignore":
            if user.id not in ignored_users:
                ignored_users.append(user.id)
                server_data["ignored_users"] = ignored_users
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await ctx.send(f"{user.mention} has been added to the ignored users list.")
            else:
                await ctx.send(f"{user.mention} is already in the ignored users list.")
            return

        if option == "listen":
            if user.id in ignored_users:
                ignored_users.remove(user.id)
                server_data["ignored_users"] = ignored_users
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await ctx.send(f"{user.mention} has been removed from the ignored users list.")
            else:
                await ctx.send(f"{user.mention} is not in the ignored users list.")
            return

        await ctx.send("Invalid option specified. Please choose either 'ignore' or 'listen'.")

    @commands.group(usage="set/on/off", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def welcome(self, ctx: commands.Context) -> None:
        await ctx.send("Invalid subcommand. Use `set`, `on`, or `off`.")

    @welcome.command(name="set", usage="join <message>/leave <message>")
    async def set_welcome_messages(
        self,
        ctx: commands.Context,
        option: str,
        *,
        message: str,
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        option = option.lower()
        if option == "join":
            update_guild_settings(self.bot.storage_path, ctx.guild.id, {"join_message": str(message)})
            await ctx.send("Join message set successfully.")
            return

        if option == "leave":
            update_guild_settings(self.bot.storage_path, ctx.guild.id, {"leave_message": str(message)})
            await ctx.send("Leave message set successfully.")
            return

        await ctx.send("Invalid option. Use `join` or `leave`.")

    @welcome.command(name="on", usage="on <channel>")
    async def set_welcome_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        update_guild_settings(self.bot.storage_path, ctx.guild.id, {"welcome_channel_ids": [str(channel.id)]})
        await ctx.send(f"Welcome channel set to {channel.mention}.")

    @welcome.command(name="off")
    async def unset_welcome_channel(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        server_data = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        if server_data.get("welcome_channel_ids"):
            server_data["welcome_channel_ids"] = []
            save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
            await ctx.send("Welcome channel unset.")
        else:
            await ctx.send("There is no welcome channel to unset.")

    @commands.command(
        name="autorole",
        help="Add or remove an autorole for new members, or apply autoroles to all non-bot members.",
        usage="add/remove/apply [role]",
    )
    @commands.has_permissions(manage_roles=True)
    async def autorole(
        self,
        ctx: commands.Context,
        action: str,
        role: Optional[discord.Role] = None,
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        server_data = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        autoroles = server_data.get("autoroles", []) or []
        action = action.lower()

        if action == "add":
            if role is None:
                await ctx.send("Please provide a role to add as an autorole.")
                return
            if role.id not in autoroles:
                autoroles.append(role.id)
                server_data["autoroles"] = autoroles
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
            await ctx.send(f"{role.name} has been added as an autorole.")
            return

        if action == "remove":
            if role is None:
                await ctx.send("Please provide a role to remove as an autorole.")
                return
            if role.id in autoroles:
                autoroles.remove(role.id)
                server_data["autoroles"] = autoroles
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await ctx.send(f"{role.name} has been removed as an autorole.")
            else:
                await ctx.send(f"{role.name} is not an autorole.")
            return

        if action == "apply":
            if role is not None:
                await ctx.send("The 'apply' action does not require a role argument.")
                return
            if not autoroles:
                await ctx.send("There are no autoroles configured.")
                return

            async with ctx.typing():
                non_bot_members = [member for member in ctx.guild.members if not member.bot]
                sleep_duration = 0.25
                estimated_time = len(non_bot_members) * len(autoroles) * sleep_duration / 60

                await ctx.send(
                    "Applying autoroles to all non-bot members. "
                    f"This action may take approximately {estimated_time:.2f} minutes. Please be patient."
                )

                for member in non_bot_members:
                    for autorole_id in autoroles:
                        autorole = ctx.guild.get_role(autorole_id)
                        if autorole is not None and autorole not in member.roles:
                            await member.add_roles(autorole)
                            import asyncio
                            await asyncio.sleep(sleep_duration)

                await ctx.send(f"{ctx.author.mention}, autoroles have been applied to all non-bot members.")
            return

        await ctx.send("Invalid action. Please use 'add', 'remove', or 'apply'.")

    def _render_setting_value(self, key: str, value: object) -> str:
        if value is None or value == "":
            return "Not set"

        if key == "logging_channel_id" and value:
            return f"<#{int(str(value))}>"

        if "channel" in key and isinstance(value, list):
            return ", ".join(f"<#{int(str(channel_id))}>" for channel_id in value) or "Not set"

        if "user" in key and isinstance(value, list):
            return ", ".join(f"<@{int(str(user_id))}>" for user_id in value) or "Not set"

        if "role" in key and isinstance(value, list):
            return ", ".join(f"<@&{int(str(role_id))}>" for role_id in value) or "Not set"

        if isinstance(value, list) and not value:
            return "Not set"

        return str(value)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(bot))
