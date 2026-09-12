"""The autovoiceset command."""

import asyncio
import io
from abc import ABC
from contextlib import suppress

import discord
from jinja2.exceptions import TemplateError
from redbot.core import Config, checks, commands
from redbot.core.utils.chat_formatting import error, info, success, warning
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu
from redbot.core.utils.predicates import MessagePredicate

from .abc import MixinMeta
from .vx_lib import SettingDisplay

channel_name_template = {
    "username": "{{username}}'s Room{% if dupenum > 1 %} ({{dupenum}}){% endif %}",
    "game": "{{game}}{% if not game %}{{username}}'s Room{% endif %}{% if dupenum > 1 %} ({{dupenum}}){% endif %}",
}

MAX_MESSAGE_LENGTH = 2000

# AutoRoom's own Config identifier - never register autovoice's schema under this.
AUTOROOM_CONFIG_IDENTIFIER = 1224364860
AUTOROOM_SCHEMA_VERSION_CURRENT = 7


class AutoVoiceSetCommands(MixinMeta, ABC):
    """The autovoiceset command."""

    @commands.group()
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def autovoiceset(self, ctx: commands.Context) -> None:
        """Configure the AutoVoice cog."""

    @autovoiceset.command()
    async def settings(self, ctx: commands.Context) -> None:
        """Display current settings."""
        if not ctx.guild:
            return
        server_section = SettingDisplay("Server Settings")
        server_section.add(
            "Admin access all AutoVoice rooms",
            await self.config.guild(ctx.guild).admin_access(),
        )
        server_section.add(
            "Moderator access all AutoVoice rooms",
            await self.config.guild(ctx.guild).mod_access(),
        )
        bot_roles = ", ".join(
            [role.name for role in await self.get_bot_roles(ctx.guild)]
        )
        if bot_roles:
            server_section.add("Bot roles allowed in all AutoVoice rooms", bot_roles)

        await ctx.send(server_section.display())
        avcs = await self.get_all_autovoice_source_configs(ctx.guild)
        for avc_id, avc_settings in avcs.items():
            source_channel = ctx.guild.get_channel(avc_id)
            if not isinstance(source_channel, discord.VoiceChannel):
                continue
            dest_category = ctx.guild.get_channel(avc_settings["dest_category_id"])
            autovoice_section = SettingDisplay(f"AutoVoice Source - {source_channel.name}")
            autovoice_section.add(
                "Room type",
                avc_settings["room_type"].capitalize(),
            )
            autovoice_section.add(
                "Destination category",
                f"#{dest_category.name}" if dest_category else "INVALID CATEGORY",
            )
            if not avc_settings["perm_send_messages"]:
                autovoice_section.add(
                    "Send Messages",
                    "False",
                )
            if not avc_settings["perm_owner_manage_channels"]:
                autovoice_section.add(
                    "Owner Manage Channel",
                    "False",
                )
            member_roles = self.get_member_roles(source_channel)
            if member_roles:
                autovoice_section.add(
                    "Member Roles" if len(member_roles) > 1 else "Member Role",
                    ", ".join(role.name for role in member_roles),
                )
            room_name_format = "Username"
            if avc_settings["channel_name_type"] in channel_name_template:
                room_name_format = avc_settings["channel_name_type"].capitalize()
            elif (
                avc_settings["channel_name_type"] == "custom"
                and avc_settings["channel_name_format"]
            ):
                room_name_format = f'Custom: "{avc_settings["channel_name_format"]}"'
            autovoice_section.add("Room name format", room_name_format)

            if avc_settings["channel_hint"]:
                autovoice_section.add(
                    "Channel Hint",
                    avc_settings["channel_hint"],
                )
            msg = autovoice_section.display()
            if len(msg) < MAX_MESSAGE_LENGTH:
                await ctx.send(msg)
            else:
                raw_msg = autovoice_section.raw()
                msg_bytes = io.BytesIO(raw_msg.encode("utf-8"))
                await ctx.send(
                    file=discord.File(msg_bytes, filename="autovoice_settings.txt")
                )

        message = ""
        required_check, optional_check, _ = await self._check_all_perms(ctx.guild)
        if not required_check:
            message += "\n" + error(
                "It looks like I am missing one or more required permissions. "
                "Until I have them, the AutoVoice cog may not function properly "
                "for all AutoVoice Sources. "
                "Check `[p]autovoiceset permissions` for more information."
            )
        elif not optional_check:
            message += "\n" + warning(
                "All AutoVoice rooms will work correctly, as I have all of the required permissions. "
                "However, it looks like I am missing one or more optional permissions "
                "for one or more AutoVoice Sources. "
                "Check `[p]autovoiceset permissions` for more information."
            )
        if message:
            await ctx.send(message)

    @autovoiceset.command(aliases=["perms"])
    async def permissions(self, ctx: commands.Context) -> None:
        """Check that the bot has all needed permissions."""
        if not ctx.guild:
            return
        required_check, optional_check, details_list = await self._check_all_perms(
            ctx.guild, detailed=True
        )
        if not details_list:
            await ctx.send(
                info(
                    "You don't have any AutoVoice Sources set up! "
                    "Set one up with `[p]autovoiceset create` first, "
                    "then I can check what permissions I need for it."
                )
            )
            return

        if (
            len(details_list) > 1
            and not ctx.channel.permissions_for(ctx.guild.me).add_reactions
        ):
            await ctx.send(
                error(
                    "Since you have multiple AutoVoice Sources, "
                    'I need the "Add Reactions" permission to display permission information.'
                )
            )
            return

        if not required_check:
            await ctx.send(
                error(
                    "It looks like I am missing one or more required permissions. "
                    "Until I have them, the AutoVoice Source(s) in question will not function properly."
                    "\n\n"
                    "The easiest way of fixing this is just giving me these permissions as part of my server role, "
                    "otherwise you will need to give me these permissions on the AutoVoice Source and destination "
                    "category, as specified below."
                )
            )
        elif not optional_check:
            await ctx.send(
                warning(
                    "It looks like I am missing one or more optional permissions. "
                    "All AutoVoice rooms will work, however some extra features may not work. "
                    "\n\n"
                    "The easiest way of fixing this is just giving me these permissions as part of my server role, "
                    "otherwise you will need to give me these permissions on the destination category, "
                    "as specified below."
                    "\n\n"
                    "In the case of optional permissions, any permission on the AutoVoice Source will be copied to "
                    "the created AutoVoice room, as if we were cloning the AutoVoice Source. In order for this to "
                    "work, I need each permission to be allowed in the destination category (or server). "
                    "If it isn't allowed, I will skip copying that permission over."
                )
            )
        else:
            await ctx.send(success("Everything looks good here!"))

        if len(details_list) > 1:
            if (
                ctx.channel.permissions_for(ctx.guild.me).add_reactions
                and ctx.channel.permissions_for(ctx.guild.me).read_message_history
            ):
                await menu(ctx, details_list, DEFAULT_CONTROLS, timeout=60.0)
            else:
                for details in details_list:
                    await ctx.send(details)
        else:
            await ctx.send(details_list[0])

    @autovoiceset.group()
    async def access(self, ctx: commands.Context) -> None:
        """Control access to all AutoVoice rooms.

        Roles that are considered "admin" or "moderator" are
        set up with the commands `[p]set addadminrole`
        and `[p]set addmodrole` (plus the remove commands too)
        """

    @access.command(name="admin")
    async def access_admin(self, ctx: commands.Context) -> None:
        """Allow Admins to join locked/private AutoVoice rooms."""
        if not ctx.guild:
            return
        admin_access = not await self.config.guild(ctx.guild).admin_access()
        await self.config.guild(ctx.guild).admin_access.set(admin_access)
        await ctx.send(
            success(
                f"Admins are {'now' if admin_access else 'no longer'} able to join (new) locked/private AutoVoice rooms."
            )
        )

    @access.command(name="mod")
    async def access_mod(self, ctx: commands.Context) -> None:
        """Allow Moderators to join locked/private AutoVoice rooms."""
        if not ctx.guild:
            return
        mod_access = not await self.config.guild(ctx.guild).mod_access()
        await self.config.guild(ctx.guild).mod_access.set(mod_access)
        await ctx.send(
            success(
                f"Moderators are {'now' if mod_access else 'no longer'} able to join (new) locked/private AutoVoice rooms."
            )
        )

    @access.group(name="bot")
    async def access_bot(self, ctx: commands.Context) -> None:
        """Automatically allow bots into AutoVoice rooms.

        The room owner is able to freely allow or deny these roles as they see fit.
        """

    @access_bot.command(name="add")
    async def access_bot_add(self, ctx: commands.Context, role: discord.Role) -> None:
        """Allow a bot role into every AutoVoice room."""
        if not ctx.guild:
            return
        bot_role_ids = await self.config.guild(ctx.guild).bot_access()
        if role.id not in bot_role_ids:
            bot_role_ids.append(role.id)
            await self.config.guild(ctx.guild).bot_access.set(bot_role_ids)

        role_list = "\n".join(
            [role.name for role in await self.get_bot_roles(ctx.guild)]
        )
        await ctx.send(
            success(
                f"AutoVoice rooms will now allow the following bot roles in by default:\n\n{role_list}"
            )
        )

    @access_bot.command(name="remove")
    async def access_bot_remove(
        self, ctx: commands.Context, role: discord.Role
    ) -> None:
        """Disallow a bot role from joining every AutoVoice room."""
        if not ctx.guild:
            return
        bot_role_ids = await self.config.guild(ctx.guild).bot_access()
        if role.id in bot_role_ids:
            bot_role_ids.remove(role.id)
            await self.config.guild(ctx.guild).bot_access.set(bot_role_ids)

        if bot_role_ids:
            role_list = "\n".join(
                [role.name for role in await self.get_bot_roles(ctx.guild)]
            )
            await ctx.send(
                success(
                    f"AutoVoice rooms will now allow the following bot roles in by default:\n\n{role_list}"
                )
            )
        else:
            await ctx.send(
                success("New AutoVoice rooms will not allow any extra bot roles in.")
            )

    @autovoiceset.command(aliases=["enable", "add"])
    async def create(
        self,
        ctx: commands.Context,
        source_voice_channel: discord.VoiceChannel,
        dest_category: discord.CategoryChannel,
    ) -> None:
        """Create an AutoVoice Source.

        Anyone joining an AutoVoice Source will automatically have a new
        voice channel created in the destination category, and then be
        moved into it.
        """
        if not ctx.guild:
            return
        perms_required, perms_optional, details = self.check_perms_source_dest(
            source_voice_channel, dest_category, detailed=True
        )
        if not perms_required or not perms_optional:
            await ctx.send(
                error(
                    "I am missing a permission that the AutoVoice cog requires me to have. "
                    "Check below for the permissions I require in both the AutoVoice Source "
                    "and the destination category. "
                    "Try creating the AutoVoice Source again once I have these permissions."
                    "\n"
                    f"{details}"
                    "\n"
                    "The easiest way of doing this is just giving me these permissions as part of my server role, "
                    "otherwise you will need to give me these permissions on the source channel and destination "
                    "category, as specified above."
                )
            )
            return
        new_source: dict[str, str | int] = {"dest_category_id": dest_category.id}

        # Room type
        options = ["public", "locked", "private", "server"]
        pred = MessagePredicate.lower_contained_in(options, ctx)
        await ctx.send(
            "**Welcome to the setup wizard for creating an AutoVoice Source!**"
            "\n"
            f"Users joining the {source_voice_channel.mention} AutoVoice Source will have a room "
            f"created in the {dest_category.mention} category and be moved into it."
            "\n\n"
            "**Room Type**"
            "\n"
            "AutoVoice rooms can be one of the following types when created:"
            "\n"
            "`public ` - Visible and joinable by other users. The room owner can kick/ban users out of them."
            "\n"
            "`locked ` - Visible, but not joinable by other users. The room owner must allow users into their room."
            "\n"
            "`private` - Not visible or joinable by other users. The room owner must allow users into their room."
            "\n"
            "`server ` - Same as a public room, but with no room owner. "
            "No modifications can be made to the generated room."
            "\n\n"
            "What would you like these created rooms to be by default? (`public`/`locked`/`private`/`server`)"
        )
        answer = None
        with suppress(asyncio.TimeoutError):
            await ctx.bot.wait_for("message", check=pred, timeout=60)
            answer = pred.result
        if answer is not None:
            new_source["room_type"] = options[answer]
        else:
            await ctx.send("No valid answer was received, canceling setup process.")
            return

        # Check perms room type
        perms_required, perms_optional, details = self.check_perms_source_dest(
            source_voice_channel,
            dest_category,
            with_manage_roles_guild=new_source["room_type"] != "server",
            detailed=True,
        )
        if not perms_required or not perms_optional:
            await ctx.send(
                error(
                    f"Since you want to have this AutoVoice Source create {new_source['room_type']} rooms, "
                    "I will need a few extra permissions. "
                    "Try creating the AutoVoice Source again once I have these permissions."
                    "\n"
                    f"{details}"
                )
            )
            return

        # Channel name
        options = ["username", "game"]
        pred = MessagePredicate.lower_contained_in(options, ctx)
        await ctx.send(
            "**Channel Name**"
            "\n"
            "When a room is created, a name will be generated for it. How would you like that name to be generated?"
            "\n\n"
            f'`username` - Shows up as "{ctx.author.display_name}\'s Room"\n'
            "`game    ` - Room owner's playing game, otherwise `username`"
        )
        answer = None
        with suppress(asyncio.TimeoutError):
            await ctx.bot.wait_for("message", check=pred, timeout=60)
            answer = pred.result
        if answer is not None:
            new_source["channel_name_type"] = options[answer]
        else:
            await ctx.send("No valid answer was received, canceling setup process.")
            return

        # Save new source
        await self.config.custom(
            "AUTOVOICE_SOURCE", str(ctx.guild.id), str(source_voice_channel.id)
        ).set(new_source)
        await ctx.send(
            success(
                "Settings saved successfully!\n"
                "Check out `[p]autovoiceset modify` for even more AutoVoice Source settings, "
                "or to make modifications to your above answers."
            )
        )

    @autovoiceset.command(aliases=["disable", "delete", "del"])
    async def remove(
        self,
        ctx: commands.Context,
        autovoice_source: discord.VoiceChannel,
    ) -> None:
        """Remove an AutoVoice Source."""
        if not ctx.guild:
            return
        await self.config.custom(
            "AUTOVOICE_SOURCE", str(ctx.guild.id), str(autovoice_source.id)
        ).clear()
        await ctx.send(
            success(
                f"**{autovoice_source.mention}** is no longer an AutoVoice Source channel."
            )
        )

    @autovoiceset.group(aliases=["edit"])
    async def modify(self, ctx: commands.Context) -> None:
        """Modify an existing AutoVoice Source."""

    @modify.command(name="category")
    async def modify_category(
        self,
        ctx: commands.Context,
        autovoice_source: discord.VoiceChannel,
        dest_category: discord.CategoryChannel,
    ) -> None:
        """Set the category that AutoVoice rooms will be created in."""
        if not ctx.guild:
            return
        if await self.get_autovoice_source_config(autovoice_source):
            await self.config.custom(
                "AUTOVOICE_SOURCE", str(ctx.guild.id), str(autovoice_source.id)
            ).dest_category_id.set(dest_category.id)
            perms_required, perms_optional, details = self.check_perms_source_dest(
                autovoice_source, dest_category, detailed=True
            )
            message = f"**{autovoice_source.mention}** will now create new rooms in the **{dest_category.mention}** category."
            if perms_required and perms_optional:
                await ctx.send(success(message))
            else:
                await ctx.send(
                    warning(
                        f"{message}"
                        "\n"
                        "Do note, this new category does not have sufficient permissions for me to make rooms. "
                        "Until you fix this, the AutoVoice Source will not work."
                        "\n"
                        f"{details}"
                    )
                )
        else:
            await ctx.send(
                error(
                    f"**{autovoice_source.mention}** is not an AutoVoice Source channel."
                )
            )

    @modify.group(name="type")
    async def modify_type(self, ctx: commands.Context) -> None:
        """Choose what type of AutoVoice room is created."""

    @modify_type.command(name="public")
    async def modify_type_public(
        self, ctx: commands.Context, autovoice_source: discord.VoiceChannel
    ) -> None:
        """Rooms will be open to all. Room owner has control over room."""
        await self._save_public_private(ctx, autovoice_source, "public")

    @modify_type.command(name="locked")
    async def modify_type_locked(
        self, ctx: commands.Context, autovoice_source: discord.VoiceChannel
    ) -> None:
        """Rooms will be visible to all, but not joinable. Room owner can allow users in."""
        await self._save_public_private(ctx, autovoice_source, "locked")

    @modify_type.command(name="private")
    async def modify_type_private(
        self, ctx: commands.Context, autovoice_source: discord.VoiceChannel
    ) -> None:
        """Rooms will be hidden. Room owner can allow users in."""
        await self._save_public_private(ctx, autovoice_source, "private")

    @modify_type.command(name="server")
    async def modify_type_server(
        self, ctx: commands.Context, autovoice_source: discord.VoiceChannel
    ) -> None:
        """Rooms will be open to all, but the server owns the room (so they can't be modified)."""
        await self._save_public_private(ctx, autovoice_source, "server")

    async def _save_public_private(
        self,
        ctx: commands.Context,
        autovoice_source: discord.VoiceChannel,
        room_type: str,
    ) -> None:
        """Save the public/private setting."""
        if not ctx.guild:
            return
        if await self.get_autovoice_source_config(autovoice_source):
            await self.config.custom(
                "AUTOVOICE_SOURCE", str(ctx.guild.id), str(autovoice_source.id)
            ).room_type.set(room_type)
            await ctx.send(
                success(
                    f"**{autovoice_source.mention}** will now create `{room_type}` rooms."
                )
            )
        else:
            await ctx.send(
                error(
                    f"**{autovoice_source.mention}** is not an AutoVoice Source channel."
                )
            )

    @modify.group(name="name")
    async def modify_name(self, ctx: commands.Context) -> None:
        """Set the default name format of an AutoVoice room."""

    @modify_name.command(name="username")
    async def modify_name_username(
        self, ctx: commands.Context, autovoice_source: discord.VoiceChannel
    ) -> None:
        """Default format: Username's Room.

        Custom format example:
        `{{username}}'s Room{% if dupenum > 1 %} ({{dupenum}}){% endif %}`
        """
        await self._save_room_name(ctx, autovoice_source, "username")

    @modify_name.command(name="game")
    async def modify_name_game(
        self, ctx: commands.Context, autovoice_source: discord.VoiceChannel
    ) -> None:
        """The users current playing game, otherwise the username format.

        Custom format example:
        `{{game}}{% if not game %}{{username}}'s Room{% endif %}{% if dupenum > 1 %} ({{dupenum}}){% endif %}`
        """  # noqa: D401
        await self._save_room_name(ctx, autovoice_source, "game")

    @modify_name.command(name="custom")
    async def modify_name_custom(
        self,
        ctx: commands.Context,
        autovoice_source: discord.VoiceChannel,
        *,
        template: str,
    ) -> None:
        """A custom channel name.

        Use `{{ expressions }}` to print variables and `{% statements %}` to do basic evaluations on variables.

        Variables supported:
        - `username` - Room owner's username
        - `game    ` - Room owner's game
        - `dupenum ` - An incrementing number that starts at 1, useful for un-duplicating channel names

        Statements supported:
        - `if/elif/else/endif`
        - Example: `{% if dupenum > 1 %}DupeNum is {{dupenum}}, which is greater than 1{% endif %}`
        - Another example: `{% if not game %}User isn't playing a game!{% endif %}`

        Check out the repo README for more info.
        """  # noqa: D401
        await self._save_room_name(ctx, autovoice_source, "custom", template)

    async def _save_room_name(
        self,
        ctx: commands.Context,
        autovoice_source: discord.VoiceChannel,
        room_type: str,
        template: str | None = None,
    ) -> None:
        """Save the room name type."""
        if not ctx.guild:
            return
        if await self.get_autovoice_source_config(autovoice_source):
            data = self.get_template_data(ctx.author)
            if template:
                template = template.replace("\n", " ")
                try:
                    # Validate template
                    await self.format_template_room_name(template, data)
                except TemplateError as rte:
                    await ctx.send(
                        error(
                            "Hmm... that doesn't seem to be a valid template:"
                            "\n\n"
                            f"`{rte!s}`"
                            "\n\n"
                            "If you need some help, take a look at the repo README."
                        )
                    )
                    return
                await self.config.custom(
                    "AUTOVOICE_SOURCE", str(ctx.guild.id), str(autovoice_source.id)
                ).channel_name_format.set(template)
            else:
                await self.config.custom(
                    "AUTOVOICE_SOURCE", str(ctx.guild.id), str(autovoice_source.id)
                ).channel_name_format.clear()
            await self.config.custom(
                "AUTOVOICE_SOURCE", str(ctx.guild.id), str(autovoice_source.id)
            ).channel_name_type.set(room_type)
            message = (
                f"New rooms created by **{autovoice_source.mention}** "
                f"will use the **{room_type.capitalize()}** format"
            )
            if template:
                message += f":\n`{template}`"
            else:
                # Load preset template for display purposes
                template = channel_name_template[room_type]
                message += "."
            if "game" not in data:
                data["game"] = "Example Game"
            message += "\n\nExample room names:"
            for room_num in range(1, 4):
                message += f"\n{await self.format_template_room_name(template, data, room_num)}"
            await ctx.send(success(message))
        else:
            await ctx.send(
                error(
                    f"**{autovoice_source.mention}** is not an AutoVoice Source channel."
                )
            )

    @modify.group(name="hint")
    async def modify_hint(
        self,
        ctx: commands.Context,
    ) -> None:
        """Configure sending an introductory message to the new AutoVoice room's chat."""

    @modify_hint.command(name="set")
    async def modify_hint_set(
        self,
        ctx: commands.Context,
        autovoice_source: discord.VoiceChannel,
        *,
        hint_text: str,
    ) -> None:
        """Send a message to the newly generated room's own chat.

        This can have template variables and statements, which you can learn more
        about by looking at `[p]autovoiceset modify name custom`, or by looking at
        the repo README.

        The only additional variable that may be useful here is the `mention` variable,
        which will insert the users mention (pinging them).

        - Example:
        `Hello {{mention}}! Welcome to your new room!`
        """
        if not ctx.guild:
            return
        if await self.get_autovoice_source_config(autovoice_source):
            data = self.get_template_data(ctx.author)
            try:
                # Validate template
                hint_text_formatted = await self.template.render(hint_text, data)
            except TemplateError as rte:
                await ctx.send(
                    error(
                        "Hmm... that doesn't seem to be a valid template:"
                        "\n\n"
                        f"`{rte!s}`"
                        "\n\n"
                        "If you need some help, take a look at the repo README."
                    )
                )
                return

            await self.config.custom(
                "AUTOVOICE_SOURCE", str(ctx.guild.id), str(autovoice_source.id)
            ).channel_hint.set(hint_text)

            await ctx.send(
                success(
                    f"New rooms created by **{autovoice_source.mention}** will have the following message sent in them:"
                    "\n\n"
                    f"{hint_text_formatted[:1900]}"
                )
            )
        else:
            await ctx.send(
                error(
                    f"**{autovoice_source.mention}** is not an AutoVoice Source channel."
                )
            )

    @modify_hint.command(name="disable")
    async def modify_hint_disable(
        self,
        ctx: commands.Context,
        autovoice_source: discord.VoiceChannel,
    ) -> None:
        """Disable sending a message to the newly generated room's own chat."""
        if not ctx.guild:
            return
        if await self.get_autovoice_source_config(autovoice_source):
            await self.config.custom(
                "AUTOVOICE_SOURCE", str(ctx.guild.id), str(autovoice_source.id)
            ).channel_hint.clear()
            await ctx.send(
                success(
                    f"New rooms created by **{autovoice_source.mention}** will no longer have a message sent in them."
                )
            )
        else:
            await ctx.send(
                error(
                    f"**{autovoice_source.mention}** is not an AutoVoice Source channel."
                )
            )

    @modify.group(name="specialperms")
    async def special_perms(self, ctx: commands.Context) -> None:
        """Modify special AutoVoice permissions.

        Remember, most permissions are automatically copied
        from the AutoVoice Source over to the room.
        These are for configuring special cases.
        """

    @special_perms.command(name="ownermodify")
    async def owner_manage_channels(
        self, ctx: commands.Context, autovoice_source: discord.VoiceChannel
    ) -> None:
        """Allow room owners to have the Manage Channels permission on their room."""
        if not ctx.guild:
            return
        if await self.get_autovoice_source_config(autovoice_source):
            new_config_value = not await self.config.custom(
                "AUTOVOICE_SOURCE", str(ctx.guild.id), str(autovoice_source.id)
            ).perm_owner_manage_channels()
            await self.config.custom(
                "AUTOVOICE_SOURCE", str(ctx.guild.id), str(autovoice_source.id)
            ).perm_owner_manage_channels.set(new_config_value)
            await ctx.send(
                success(
                    f"Room owners are {'now' if new_config_value else 'no longer'} able to modify their room with native Discord controls."
                )
            )
        else:
            await ctx.send(
                error(
                    f"**{autovoice_source.mention}** is not an AutoVoice Source channel."
                )
            )

    @special_perms.command(name="sendmessage")
    async def public_send_messages(
        self, ctx: commands.Context, autovoice_source: discord.VoiceChannel
    ) -> None:
        """Allow users to send messages in the room's own chat."""
        if not ctx.guild:
            return
        if await self.get_autovoice_source_config(autovoice_source):
            new_config_value = not await self.config.custom(
                "AUTOVOICE_SOURCE", str(ctx.guild.id), str(autovoice_source.id)
            ).perm_send_messages()
            await self.config.custom(
                "AUTOVOICE_SOURCE", str(ctx.guild.id), str(autovoice_source.id)
            ).perm_send_messages.set(new_config_value)
            await ctx.send(
                success(
                    f"Users are {'now' if new_config_value else 'no longer'} able to send messages in the room's own chat."
                )
            )
        else:
            await ctx.send(
                error(
                    f"**{autovoice_source.mention}** is not an AutoVoice Source channel."
                )
            )

    @modify.command(
        name="defaults", aliases=["bitrate", "memberrole", "other", "perms", "users"]
    )
    async def modify_defaults(self, ctx: commands.Context) -> None:
        """Learn how AutoVoice defaults are set."""
        await ctx.send(
            info(
                "**Bitrate/User Limit**"
                "\n"
                "Default bitrate and user limit settings are copied from the AutoVoice Source to the resulting room."
                "\n\n"
                "**Member Roles**"
                "\n"
                "Only members that can view and join an AutoVoice Source will be able to join its resulting rooms. "
                "If you would like to limit rooms to only allow certain members, simply deny the everyone role "
                "from viewing/connecting to the AutoVoice Source and allow your member roles to view/connect to it."
                "\n\n"
                "**Permissions**"
                "\n"
                "All permission overwrites (except for Manage Roles) will be copied from the AutoVoice Source "
                "to the resulting room. Every permission overwrite you want copied over, regardless if it is "
                "allowed or denied, must be allowed for the bot. It can either be allowed for the bot in the "
                "destination category or server-wide with the roles it has. `[p]autovoiceset permissions` will "
                "show what permissions will be copied over."
            )
        )

    async def _check_all_perms(
        self, guild: discord.Guild, *, detailed: bool = False
    ) -> tuple[bool, bool, list[str]]:
        """Check all permissions for all AutoVoice rooms in a guild."""
        result_required = True
        result_optional = True
        result_list = []
        avcs = await self.get_all_autovoice_source_configs(guild)
        for avc_id, avc_settings in avcs.items():
            autovoice_source = guild.get_channel(avc_id)
            category_dest = guild.get_channel(avc_settings["dest_category_id"])
            if isinstance(autovoice_source, discord.VoiceChannel) and isinstance(
                category_dest, discord.CategoryChannel
            ):
                (
                    required_check,
                    optional_check,
                    detail,
                ) = self.check_perms_source_dest(
                    autovoice_source,
                    category_dest,
                    with_manage_roles_guild=avc_settings["room_type"] != "server",
                    with_optional_clone_perms=True,
                    detailed=detailed,
                )
                result_required = result_required and required_check
                result_optional = result_optional and optional_check
                if detailed:
                    result_list.append(detail)
                elif not result_required and not result_optional:
                    break
        return result_required, result_optional, result_list

    #
    # AutoRoom migration
    #

    @autovoiceset.command()
    async def migrate(self, ctx: commands.Context) -> None:
        """Migrate this server's AutoRoom setup into AutoVoice.

        This reads AutoRoom's own stored configuration directly (it does not
        require AutoRoom to be loaded, though AutoRoom should have been loaded
        at least once recently so its own migrations have run). It copies over:

        - The admin/mod/bot access settings for this server.
        - Every AutoRoom Source in this server (as an AutoVoice Source),
          minus the legacy text channel feature, which AutoVoice does not support.
        - Every currently open AutoRoom-created voice channel, so it keeps
          its owner and deny list under AutoVoice without waiting for it
          to empty out first.

        Running this multiple times is safe. Once you've confirmed everything
        works, `[p]unload autoroom` to fully retire the old cog.
        """
        if not ctx.guild:
            return

        # A read-only handle onto AutoRoom's own Config. force_registration=False
        # so we don't blow up if AutoRoom isn't installed, but we still register
        # AutoRoom's exact original defaults below so reads of unset keys come
        # back correct instead of Config's own blank defaults.
        autoroom_config = Config.get_conf(
            cog_instance=None,
            identifier=AUTOROOM_CONFIG_IDENTIFIER,
            force_registration=False,
            cog_name="AutoRoom",
        )
        autoroom_config.register_global(schema_version=0)
        autoroom_config.register_guild(
            admin_access=True,
            mod_access=False,
            bot_access=[],
        )
        autoroom_config.init_custom("AUTOROOM_SOURCE", 2)
        autoroom_config.register_custom(
            "AUTOROOM_SOURCE",
            dest_category_id=None,
            room_type="public",
            legacy_text_channel=False,
            text_channel_hint=None,
            text_channel_topic="",
            channel_name_type="username",
            channel_name_format="",
            perm_owner_manage_channels=True,
            perm_send_messages=True,
        )
        autoroom_config.register_channel(
            source_channel=None,
            owner=None,
            associated_text_channel=None,
            denied=[],
        )

        warnings: list[str] = []

        schema_version = await autoroom_config.schema_version()
        if schema_version < AUTOROOM_SCHEMA_VERSION_CURRENT:
            warnings.append(
                f"AutoRoom's stored schema version is `{schema_version}` (expected "
                f"`{AUTOROOM_SCHEMA_VERSION_CURRENT}` or higher). Its data may be in an "
                "older shape. Make sure AutoRoom has been loaded at least once recently "
                "(it self-migrates on load) before trusting this migration - proceeding anyway."
            )

        # Guild-level access settings
        old_guild_conf = autoroom_config.guild(ctx.guild)
        await self.config.guild(ctx.guild).admin_access.set(
            await old_guild_conf.admin_access()
        )
        await self.config.guild(ctx.guild).mod_access.set(
            await old_guild_conf.mod_access()
        )
        await self.config.guild(ctx.guild).bot_access.set(
            await old_guild_conf.bot_access()
        )

        # Per-source migration
        old_sources = await autoroom_config.custom(
            "AUTOROOM_SOURCE", str(ctx.guild.id)
        ).all()
        migrated_source_ids: set[int] = set()
        skipped_sources: list[str] = []
        dropped_features: list[str] = []
        for channel_id_str, old_source in old_sources.items():
            try:
                channel_id = int(channel_id_str)
            except ValueError:
                continue
            source_channel = ctx.guild.get_channel(channel_id)
            if not isinstance(source_channel, discord.VoiceChannel):
                skipped_sources.append(
                    f"`{channel_id}` (channel no longer exists in this server)"
                )
                continue

            new_source = {
                "dest_category_id": old_source.get("dest_category_id"),
                "room_type": old_source.get("room_type", "public"),
                "channel_name_type": old_source.get("channel_name_type", "username"),
                "channel_name_format": old_source.get("channel_name_format", ""),
                "channel_hint": old_source.get("text_channel_hint"),
                "perm_owner_manage_channels": old_source.get(
                    "perm_owner_manage_channels", True
                ),
                "perm_send_messages": old_source.get("perm_send_messages", True),
            }
            await self.config.custom(
                "AUTOVOICE_SOURCE", str(ctx.guild.id), str(channel_id)
            ).set(new_source)
            migrated_source_ids.add(channel_id)

            if old_source.get("legacy_text_channel") or old_source.get(
                "text_channel_topic"
            ):
                dropped_features.append(
                    f"Source {source_channel.mention} had a legacy text channel enabled - "
                    "this feature was not ported to AutoVoice; its topic/companion channel "
                    "will no longer be created."
                )

        # Live room adoption: keep already-open AutoRoom-created rooms working
        adopted = 0
        for voice_channel in ctx.guild.voice_channels:
            old_source_channel_id = await autoroom_config.channel(
                voice_channel
            ).source_channel()
            if not old_source_channel_id:
                continue
            already_a_source = bool(
                await self.config.custom(
                    "AUTOVOICE_SOURCE", str(ctx.guild.id), str(old_source_channel_id)
                ).dest_category_id()
            )
            if old_source_channel_id not in migrated_source_ids and not already_a_source:
                # This room's source wasn't (and isn't) a known AutoVoice Source, skip it
                continue
            old_channel_conf = autoroom_config.channel(voice_channel)
            await self.config.channel(voice_channel).source_channel.set(
                old_source_channel_id
            )
            await self.config.channel(voice_channel).owner.set(
                await old_channel_conf.owner()
            )
            await self.config.channel(voice_channel).denied.set(
                await old_channel_conf.denied()
            )
            adopted += 1

        # Summary
        summary = SettingDisplay("AutoRoom -> AutoVoice migration")
        summary.add("Sources migrated", len(migrated_source_ids))
        summary.add("Live rooms adopted", adopted)
        summary.add("Sources skipped", len(skipped_sources))
        message = str(summary)
        if skipped_sources:
            message += "\n\n**Skipped sources:**\n" + "\n".join(
                f"- {line}" for line in skipped_sources
            )
        if dropped_features:
            message += "\n\n**Dropped features:**\n" + "\n".join(
                f"- {line}" for line in dropped_features
            )
        if warnings:
            message += "\n\n" + "\n".join(warning(line) for line in warnings)
        message += (
            "\n\n"
            + info(
                "Once you've confirmed AutoVoice is working correctly, run "
                "`[p]unload autoroom` to fully retire the old cog. This command "
                "does not do that for you."
            )
        )
        for chunk_start in range(0, len(message), MAX_MESSAGE_LENGTH):
            await ctx.send(message[chunk_start : chunk_start + MAX_MESSAGE_LENGTH])
