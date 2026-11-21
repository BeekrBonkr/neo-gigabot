from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.numbers import is_digits
from utils import rolemenus as store
from utils.checks import server_permissions
from utils.modules import module_enabled

LOGGER = logging.getLogger(__name__)
BUTTON_PREFIX, SELECT_ID = "rolemenu:btn:", "rolemenu:select"


def build_view(roles: list[discord.Role], style: str, mode: str) -> discord.ui.View:
    """The components for a menu message. The custom IDs are all the bot needs
    to handle a press later, so the menu keeps working after a restart."""
    view = discord.ui.View(timeout=None)
    if style == "dropdown":
        view.add_item(discord.ui.Select(
            custom_id=SELECT_ID, placeholder="Choose your role" if mode == "single" else "Choose your roles",
            min_values=0, max_values=1 if mode == "single" else len(roles),
            options=[discord.SelectOption(label=role.name[:100], value=str(role.id)) for role in roles],
        ))
    else:
        for role in roles:
            view.add_item(discord.ui.Button(label=role.name[:80], style=discord.ButtonStyle.secondary, custom_id=f"{BUTTON_PREFIX}{role.id}"))
    return view


def menu_hint(style: str, mode: str) -> str:
    hint = "Press a button to get a role; press it again to remove it." if style == "buttons" else "Open the menu and pick. Unpick a role to remove it."
    return hint + (" You can have one of these at a time." if mode == "single" else "")


class RoleMenus(commands.Cog):
    """Messages with buttons or a dropdown that members press to give
    themselves roles, with an optional "only one of these" mode."""

    rolemenu = app_commands.Group(
        name="rolemenu", description="Role menus with buttons or a dropdown.", guild_only=True,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _check_roles(self, guild: discord.Guild, invoker: discord.Member, role_ids: list[int]) -> list[discord.Role]:
        """The roles for a new menu, or RoleMenuError saying which one can't be used and why."""
        if not role_ids:
            raise store.RoleMenuError("Mention the roles to offer, like `@Red @Blue @Green`.")
        if len(role_ids) > store.MAX_ROLES_PER_MENU:
            raise store.RoleMenuError(f"A menu can offer up to {store.MAX_ROLES_PER_MENU} roles.")
        me, roles = guild.me, []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None or role.is_default():
                raise store.RoleMenuError("One of those roles doesn't exist here.")
            if role.managed:
                raise store.RoleMenuError(f"{role.name} is managed by an integration and can't be handed out.")
            if any(getattr(role.permissions, name) for name in store.ELEVATED):
                raise store.RoleMenuError(f"{role.name} has moderation or admin permissions, so members can't give it to themselves.")
            if me is None or role >= me.top_role:
                raise store.RoleMenuError(f"{role.name} is above my highest role, so I can't give it out. Move my role above it.")
            if guild.owner_id != invoker.id and role >= invoker.top_role:
                raise store.RoleMenuError(f"{role.name} is not below your own highest role.")
            roles.append(role)
        return roles

    @rolemenu.command(name="create", description="Post a message members use to give themselves roles.")
    @app_commands.describe(
        roles="The roles to offer: mention them, like @Red @Blue @Green",
        title="Heading of the message",
        style="Buttons (one per role) or a dropdown",
        mode="Whether members can hold several of these roles or only one",
        description="Optional text under the heading",
        channel="Where to post it (defaults to this channel)",
    )
    @app_commands.choices(
        style=[app_commands.Choice(name="Buttons", value="buttons"), app_commands.Choice(name="Dropdown", value="dropdown")],
        mode=[app_commands.Choice(name="Any number of these roles", value="multiple"), app_commands.Choice(name="Only one of these roles", value="single")],
    )
    @server_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def create(self, interaction: discord.Interaction, roles: str, title: app_commands.Range[str, 1, 200] = "Pick your roles",
                     style: app_commands.Choice[str] | None = None, mode: app_commands.Choice[str] | None = None,
                     description: app_commands.Range[str, 1, 1000] | None = None, channel: discord.TextChannel | None = None) -> None:
        style_value, mode_value = (style.value if style else "buttons"), (mode.value if mode else "multiple")
        target = channel or interaction.channel
        try:
            if store.count_menus(self.bot.storage_path, interaction.guild.id) >= store.MAX_MENUS_PER_GUILD:
                raise store.RoleMenuError(f"This server already has {store.MAX_MENUS_PER_GUILD} role menus. Delete one with `/rolemenu delete`.")
            chosen = self._check_roles(interaction.guild, interaction.user, store.parse_role_ids(roles))
        except store.RoleMenuError as exc:
            await self.bot.embeds.error_interaction(interaction, "Role Menu Not Created", str(exc), ephemeral=True)
            return
        try:
            message = await target.send(embed=self.menu_embed(title, description or "", style_value, mode_value), view=build_view(chosen, style_value, mode_value))
        except (discord.HTTPException, AttributeError):
            await self.bot.embeds.error_interaction(interaction, "Could Not Post", "I can't send messages in that channel.", ephemeral=True)
            return
        menu_id = store.add_menu(self.bot.storage_path, interaction.guild.id, target.id, message.id, title, style_value, mode_value,
                                 [role.id for role in chosen], interaction.user.id, description or "")
        await self.bot.embeds.success_interaction(
            interaction, "Role Menu Posted", f"Menu `#{menu_id}` with {len(chosen)} role(s) is up in {target.mention}: {message.jump_url}", ephemeral=True
        )

    def menu_embed(self, title: str, description: str, style: str, mode: str) -> discord.Embed:
        return self.bot.embeds.info_embed(f"🎭 {title}", (description + "\n\n" if description else "") + menu_hint(style, mode))

    async def apply_edit(self, guild: discord.Guild, editor: discord.Member, menu_id: int, *, title: str | None = None, description: str | None = None,
                         style: str | None = None, mode: str | None = None, role_ids: list[int] | None = None) -> dict:
        """Change an existing menu in place: its message is edited, so members
        keep the roles they picked and the link to it stays valid. Anything
        left as None is kept. Raises RoleMenuError with a message for the admin."""
        menu = store.get_menu(self.bot.storage_path, guild.id, menu_id)
        if menu is None:
            raise store.RoleMenuError("There is no role menu with that number here.")
        new = {
            "title": (title if title is not None else menu["title"]).strip()[:200] or menu["title"],
            "description": (description if description is not None else menu.get("description") or "").strip()[:1000],
            "style": style or menu["style"], "mode": mode or menu["mode"],
            "role_ids": list(role_ids) if role_ids is not None else menu["role_ids"],
        }
        if new["style"] not in store.STYLES or new["mode"] not in store.MODES:
            raise store.RoleMenuError("Choose buttons or dropdown, and any-number or one-at-a-time.")
        if role_ids is None:
            # Roles deleted since the menu was made are dropped rather than blocking the edit.
            new["role_ids"] = [r for r in new["role_ids"] if guild.get_role(r) is not None]
        roles = self._check_roles(guild, editor, new["role_ids"])
        channel = guild.get_channel(menu["channel_id"])
        if channel is None:
            raise store.RoleMenuError("The channel this menu was posted in no longer exists. Delete the menu and post a new one.")
        try:
            await channel.get_partial_message(menu["message_id"]).edit(
                embed=self.menu_embed(new["title"], new["description"], new["style"], new["mode"]), view=build_view(roles, new["style"], new["mode"]))
        except discord.NotFound:
            raise store.RoleMenuError("The menu's message was deleted in Discord. Delete the menu and post a new one.") from None
        except discord.HTTPException as exc:
            raise store.RoleMenuError(f"Discord refused the edit: {exc}") from exc
        store.update_menu(self.bot.storage_path, guild.id, menu_id, **new)
        return {**menu, **new}

    @rolemenu.command(name="edit", description="Change a role menu in place. Leave an option out to keep it as it is.")
    @app_commands.describe(
        menu_id="The menu's number, shown in /rolemenu list",
        roles="The full new list of roles: mention them, like @Red @Blue @Green",
        title="New heading", description="New text under the heading (type none to clear it)",
        style="Buttons or a dropdown", mode="Whether members can hold several of these roles or only one",
    )
    @app_commands.choices(
        style=[app_commands.Choice(name="Buttons", value="buttons"), app_commands.Choice(name="Dropdown", value="dropdown")],
        mode=[app_commands.Choice(name="Any number of these roles", value="multiple"), app_commands.Choice(name="Only one of these roles", value="single")],
    )
    @server_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def edit(self, interaction: discord.Interaction, menu_id: int, roles: str | None = None, title: app_commands.Range[str, 1, 200] | None = None,
                   description: app_commands.Range[str, 1, 1000] | None = None, style: app_commands.Choice[str] | None = None,
                   mode: app_commands.Choice[str] | None = None) -> None:
        if description is not None and description.strip().lower() == "none":
            description = ""
        try:
            menu = await self.apply_edit(
                interaction.guild, interaction.user, menu_id, title=title, description=description, style=style.value if style else None,
                mode=mode.value if mode else None, role_ids=store.parse_role_ids(roles) if roles is not None else None,
            )
        except store.RoleMenuError as exc:
            await self.bot.embeds.error_interaction(interaction, "Role Menu Not Changed", str(exc), ephemeral=True)
            return
        await self.bot.embeds.success_interaction(
            interaction, "Role Menu Updated",
            f"Menu `#{menu_id}` now offers {len(menu['role_ids'])} role(s) as {menu['style']}. Members keep the roles they already picked, "
            "including any you removed from the menu.", ephemeral=True,
        )

    @rolemenu.command(name="list", description="List this server's role menus.")
    @server_permissions(manage_roles=True)
    async def list_menus(self, interaction: discord.Interaction) -> None:
        menus = store.list_menus(self.bot.storage_path, interaction.guild.id)
        lines = [
            f"`#{menu['id']}` **{menu['title']}** in <#{menu['channel_id']}> · {menu['style']}, {menu['mode']} · " + " ".join(f"<@&{r}>" for r in menu["role_ids"])
            for menu in menus
        ] or ["No role menus yet. Make one with `/rolemenu create`."]
        await self.bot.embeds.respond(interaction, embed=self.bot.embeds.info_embed(f"Role Menus ({len(menus)})", "\n".join(lines)[:4000]), ephemeral=True)

    @rolemenu.command(name="delete", description="Delete a role menu and its message.")
    @app_commands.describe(menu_id="The menu's number, shown in /rolemenu list")
    @server_permissions(manage_roles=True)
    async def delete(self, interaction: discord.Interaction, menu_id: int) -> None:
        menu = store.delete_menu(self.bot.storage_path, interaction.guild.id, menu_id)
        if menu is None:
            await self.bot.embeds.error_interaction(interaction, "Not Found", "There is no role menu with that number here.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(menu["channel_id"])
        try:
            await (await channel.fetch_message(menu["message_id"])).delete()
        except (discord.HTTPException, AttributeError):
            pass  # the message is already gone
        await self.bot.embeds.success_interaction(interaction, "Role Menu Deleted", f"Deleted menu `#{menu_id}`. Members keep the roles they picked.", ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component or interaction.guild is None or interaction.message is None:
            return
        custom_id = str((interaction.data or {}).get("custom_id", ""))
        if not (custom_id.startswith(BUTTON_PREFIX) or custom_id == SELECT_ID):
            return
        try:
            await self.handle_press(interaction, custom_id)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Role menu press failed in guild %s", interaction.guild.id)

    async def handle_press(self, interaction: discord.Interaction, custom_id: str) -> None:
        member, guild = interaction.user, interaction.guild
        if not module_enabled(self.bot.storage_path, guild.id, "rolemenus"):
            await interaction.response.send_message("Role menus are turned off in this server right now.", ephemeral=True)
            return
        menu = store.get_by_message(self.bot.storage_path, interaction.message.id)
        if menu is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("This role menu is no longer active.", ephemeral=True)
            return
        if custom_id == SELECT_ID:
            chosen, toggle = [int(v) for v in (interaction.data or {}).get("values", []) if is_digits(str(v))], False
        else:
            raw = custom_id[len(BUTTON_PREFIX):]
            chosen, toggle = ([int(raw)] if is_digits(raw) else []), True
        add_ids, remove_ids = store.plan_changes(menu["role_ids"], {role.id for role in member.roles}, chosen, mode=menu["mode"], toggle=toggle)

        def usable(role_id: int) -> discord.Role | None:
            role = guild.get_role(role_id)
            return role if role is not None and not role.managed and guild.me is not None and role < guild.me.top_role else None

        to_add, to_remove = [r for r in map(usable, add_ids) if r], [r for r in map(usable, remove_ids) if r]
        try:
            if to_remove:
                await member.remove_roles(*to_remove, reason="Role menu")
            if to_add:
                await member.add_roles(*to_add, reason="Role menu")
        except discord.HTTPException:
            await interaction.response.send_message("I couldn't change your roles. Tell a moderator: my role may be too low, or I lost Manage Roles.", ephemeral=True)
            return
        parts = ([f"Added {', '.join(r.mention for r in to_add)}"] if to_add else []) + ([f"Removed {', '.join(r.mention for r in to_remove)}"] if to_remove else [])
        skipped = len(add_ids) + len(remove_ids) - len(to_add) - len(to_remove)
        text = ". ".join(parts) + "." if parts else "Nothing changed."
        if skipped:
            text += f" {skipped} role(s) couldn't be changed because they no longer exist or are above my role."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoleMenus(bot))
