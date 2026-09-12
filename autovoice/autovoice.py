"""AutoVoice cog: automatic voice channel creation and management."""

import random
from abc import ABC
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, ClassVar

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import humanize_timedelta

from .c_autovoice import AutoVoiceCommands
from .c_autovoiceset import AutoVoiceSetCommands, channel_name_template
from .vx_lib import Perms, SettingDisplay
from .vx_template import Template


class CompositeMetaClass(type(commands.Cog), type(ABC)):
    """Allows the metaclass used for proper type detection to coexist with discord.py's metaclass."""


class AutoVoice(
    AutoVoiceCommands,
    AutoVoiceSetCommands,
    commands.Cog,
    metaclass=CompositeMetaClass,
):
    """Automatic voice channel creation and management.

    This cog facilitates automatic voice channel creation.
    When a member joins an AutoVoice Source (voice channel),
    this cog will move them to a brand new voice channel that they have
    control over. Once everyone leaves the room, it is automatically deleted.

    This is a from-scratch reimplementation of PhasecoreX's AutoRoom cog,
    with the optional legacy companion text channel feature dropped (it is
    made obsolete by Discord's own voice channel text chat). Use
    `[p]autovoiceset migrate` to bring over an existing AutoRoom setup.
    """

    __author__ = "Poag"
    __version__ = "1.0.0"

    default_global_settings: ClassVar[dict[str, int]] = {"schema_version": 1}
    default_guild_settings: ClassVar[dict[str, bool | list[int]]] = {
        "admin_access": True,
        "mod_access": False,
        "bot_access": [],
    }
    default_autovoice_source_settings: ClassVar[dict[str, int | str | None]] = {
        "dest_category_id": None,
        "room_type": "public",
        "channel_name_type": "username",
        "channel_name_format": "",
        "channel_hint": None,
        "perm_owner_manage_channels": True,
        "perm_send_messages": True,
    }
    default_channel_settings: ClassVar[dict[str, int | list[int] | None]] = {
        "source_channel": None,
        "owner": None,
        "denied": [],
    }
    extra_channel_name_change_delay = 4

    perms_bot_source: ClassVar[dict[str, bool]] = {
        "view_channel": True,
        "connect": True,
        "move_members": True,
    }
    perms_bot_dest: ClassVar[dict[str, bool]] = {
        "view_channel": True,
        "connect": True,
        "send_messages": True,
        "manage_channels": True,
        "manage_messages": True,
        "move_members": True,
    }

    def __init__(self, bot: Red) -> None:
        """Set up the cog."""
        super().__init__()
        self.bot = bot
        # Pick a random identifier distinct from AutoRoom's own (1224364860),
        # which c_autovoiceset.py reads from directly (read-only) for migration.
        self.config = Config.get_conf(
            self, identifier=741852963, force_registration=True
        )
        self.config.register_global(**self.default_global_settings)
        self.config.register_guild(**self.default_guild_settings)
        self.config.init_custom("AUTOVOICE_SOURCE", 2)
        self.config.register_custom(
            "AUTOVOICE_SOURCE", **self.default_autovoice_source_settings
        )
        self.config.register_channel(**self.default_channel_settings)
        self.template = Template()
        self.bucket_autovoice_create = commands.CooldownMapping.from_cooldown(
            2, 60, lambda member: member
        )
        self.bucket_autovoice_create_warn = commands.CooldownMapping.from_cooldown(
            1, 3600, lambda member: member
        )
        self.bucket_autovoice_name = commands.CooldownMapping.from_cooldown(
            2, 600 + self.extra_channel_name_change_delay, lambda channel: channel
        )
        self.bucket_autovoice_owner_claim = commands.CooldownMapping.from_cooldown(
            1, 120, lambda channel: channel
        )

    #
    # Red methods
    #

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """Show version in help."""
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nCog Version: {self.__version__}"

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Remove a user's ownership/deny-list references from every AutoVoice room's config."""
        all_channels = await self.config.all_channels()
        for channel_id, channel_settings in all_channels.items():
            if channel_settings.get("owner") == user_id:
                await self.config.channel_from_id(channel_id).owner.set(None)
            denied = channel_settings.get("denied") or []
            if user_id in denied:
                async with self.config.channel_from_id(channel_id).denied() as d:
                    if user_id in d:
                        d.remove(user_id)

    #
    # Initialization methods
    #

    async def initialize(self) -> None:
        """Perform setup actions before loading cog."""
        self.bot.loop.create_task(self._cleanup_autovoice_rooms())

    async def _cleanup_autovoice_rooms(self) -> None:
        """Remove non-existent AutoVoice rooms from the config."""
        await self.bot.wait_until_ready()
        voice_channel_dict = await self.config.all_channels()
        for voice_channel_id in voice_channel_dict:
            voice_channel = self.bot.get_channel(voice_channel_id)
            if voice_channel:
                if isinstance(voice_channel, discord.VoiceChannel):
                    # Delete AutoVoice room if it is empty
                    await self._process_autovoice_delete(voice_channel, None)
            else:
                # Room has already been deleted, just clean up its config
                await self.config.channel_from_id(voice_channel_id).clear()

    #
    # Listener methods
    #

    @commands.Cog.listener()
    async def on_guild_channel_delete(
        self, guild_channel: discord.abc.GuildChannel
    ) -> None:
        """Clean up config when an AutoVoice room (or Source) is deleted (either by the bot or the user)."""
        if not isinstance(guild_channel, discord.VoiceChannel):
            return
        if await self.get_autovoice_source_config(guild_channel):
            # AutoVoice Source was deleted, remove configuration
            await self.config.custom(
                "AUTOVOICE_SOURCE", str(guild_channel.guild.id), str(guild_channel.id)
            ).clear()
        else:
            # Room was deleted, remove its config
            await self.config.channel(guild_channel).clear()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        leaving: discord.VoiceState,
        joining: discord.VoiceState,
    ) -> None:
        """Do voice channel stuff when users move about channels."""
        if await self.bot.cog_disabled_in_guild(self, member.guild):
            return

        if leaving.channel == joining.channel:
            return

        # If user left an AutoVoice room, do cleanup
        if isinstance(leaving.channel, discord.VoiceChannel):
            autovoice_info = await self.get_autovoice_info(leaving.channel)
            if autovoice_info:
                deleted = await self._process_autovoice_delete(leaving.channel, member)
                if not deleted and member.id == autovoice_info["owner"]:
                    # There are still users left and the room owner left.
                    # Start a countdown so that others can claim the room.
                    bucket = self.bucket_autovoice_owner_claim.get_bucket(
                        leaving.channel
                    )
                    if bucket:
                        bucket.reset()
                        bucket.update_rate_limit()

        if isinstance(joining.channel, discord.VoiceChannel):
            # If user entered an AutoVoice Source channel, create a new room
            asc = await self.get_autovoice_source_config(joining.channel)
            if asc:
                await self._process_autovoice_create(joining.channel, asc, member)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Check joining users against existing AutoVoice rooms, re-adds their deny override if missing."""
        for autovoice_channel in member.guild.voice_channels:
            autovoice_info = await self.get_autovoice_info(autovoice_channel)
            if autovoice_info and member.id in autovoice_info["denied"]:
                source_channel = member.guild.get_channel(
                    autovoice_info["source_channel"]
                )
                asc = await self.get_autovoice_source_config(source_channel)
                if not asc:
                    continue
                perms = Perms(autovoice_channel.overwrites)
                perms.update(member, asc["perms"]["deny"])
                if perms.modified:
                    await autovoice_channel.edit(
                        overwrites=perms.overwrites or {},
                        reason="AutoVoice: Rejoining user, prevent deny evasion",
                    )

    #
    # Private methods
    #

    async def _process_autovoice_create(
        self,
        autovoice_source: discord.VoiceChannel,
        autovoice_source_config: dict[str, Any],
        member: discord.Member,
    ) -> None:
        """Create a voice channel for a member in an AutoVoice Source channel."""
        # Check perms for guild, source, and dest
        guild = autovoice_source.guild
        dest_category = guild.get_channel(autovoice_source_config["dest_category_id"])
        if not isinstance(dest_category, discord.CategoryChannel):
            return
        required_check, optional_check, _ = self.check_perms_source_dest(
            autovoice_source, dest_category
        )
        if not required_check or not optional_check:
            return

        # Check that user isn't spamming
        bucket = self.bucket_autovoice_create.get_bucket(member)
        if bucket:
            retry_after = bucket.update_rate_limit()
            if retry_after:
                warn_bucket = self.bucket_autovoice_create_warn.get_bucket(member)
                if warn_bucket:
                    if not warn_bucket.update_rate_limit():
                        with suppress(
                            discord.Forbidden,
                            discord.NotFound,
                            discord.HTTPException,
                        ):
                            await member.send(
                                "Hello there! It looks like you're trying to make an AutoVoice room."
                                "\n"
                                f"Please note that you are only allowed to make **{bucket.rate}** rooms "
                                f"every **{humanize_timedelta(seconds=bucket.per)}**."
                                "\n"
                                f"You can try again in **{humanize_timedelta(seconds=max(retry_after, 1))}**."
                            )
                    return

        # Generate channel name
        taken_channel_names = [
            voice_channel.name for voice_channel in dest_category.voice_channels
        ]
        new_channel_name = await self._generate_channel_name(
            autovoice_source_config, member, taken_channel_names
        )

        # Generate overwrites
        perms = Perms()
        dest_perms = dest_category.permissions_for(dest_category.guild.me)
        source_overwrites = autovoice_source.overwrites or {}
        member_roles = self.get_member_roles(autovoice_source)
        for target, permissions in source_overwrites.items():
            # We can't put manage_roles in overwrites, so just get rid of it
            permissions.update(manage_roles=None)
            # Check each permission for each overwrite target to make sure the bot has it allowed in the dest category
            failed_checks = {}
            for name, value in permissions:
                if value is not None:
                    permission_check_result = getattr(dest_perms, name)
                    if not permission_check_result:
                        # If the bot doesn't have the permission allowed in the dest category, just ignore it. Too bad!
                        failed_checks[name] = None
            if failed_checks:
                permissions.update(**failed_checks)
            perms.overwrite(target, permissions)
            if member_roles and target in member_roles:
                # If we have member roles and this target is one, apply AutoVoice type permissions
                perms.update(target, autovoice_source_config["perms"]["access"])

        # Update overwrites for default role to account for AutoVoice type
        if member_roles:
            perms.update(guild.default_role, autovoice_source_config["perms"]["deny"])
        else:
            perms.update(
                guild.default_role, autovoice_source_config["perms"]["access"]
            )

        # Bot overwrites
        perms.update(guild.me, self.perms_bot_dest)

        # Room owner overwrites
        if autovoice_source_config["room_type"] != "server":
            perms.update(member, autovoice_source_config["perms"]["owner"])

        # Admin/moderator/bot overwrites
        # Add bot roles to be allowed
        additional_allowed_roles = await self.get_bot_roles(guild)
        if await self.config.guild(guild).mod_access():
            # Add mod roles to be allowed
            additional_allowed_roles += await self.bot.get_mod_roles(guild)
        if await self.config.guild(guild).admin_access():
            # Add admin roles to be allowed
            additional_allowed_roles += await self.bot.get_admin_roles(guild)
        for role in additional_allowed_roles:
            # Add all the mod/admin roles, if required
            perms.update(role, autovoice_source_config["perms"]["allow"])

        # Create new AutoVoice room
        voice_channel_config = {
            "name": new_channel_name,
            "category": dest_category,
            "reason": "AutoVoice: New room needed.",
            "bitrate": min(autovoice_source.bitrate, int(guild.bitrate_limit)),
            "user_limit": autovoice_source.user_limit,
        }
        if perms.overwrites:
            voice_channel_config["overwrites"] = perms.overwrites
        if autovoice_source.rtc_region:
            voice_channel_config["rtc_region"] = autovoice_source.rtc_region
        new_voice_channel = await guild.create_voice_channel(**voice_channel_config)
        await self.config.channel(new_voice_channel).source_channel.set(
            autovoice_source.id
        )
        if autovoice_source_config["room_type"] != "server":
            await self.config.channel(new_voice_channel).owner.set(member.id)
        try:
            await member.move_to(
                new_voice_channel, reason="AutoVoice: Move user to new room."
            )
        except discord.HTTPException:
            await self._process_autovoice_delete(new_voice_channel, member)
            return

        # Send chat hint if enabled - straight into the room's own built-in chat
        if autovoice_source_config["channel_hint"]:
            with suppress(Exception):
                hint = await self.template.render(
                    autovoice_source_config["channel_hint"],
                    self.get_template_data(member),
                )
                if hint:
                    await new_voice_channel.send(hint[:2000].strip())

    @staticmethod
    async def _process_autovoice_delete(
        voice_channel: discord.VoiceChannel, leaving_user: discord.Member | None
    ) -> bool:
        """Delete an AutoVoice room if empty."""
        if (
            # If there are no members left in the channel, or if there's just one and it's the person currently leaving (race condition)
            not voice_channel.members
            or (
                len(voice_channel.members) == 1
                and voice_channel.members[0] == leaving_user
            )
        ) and voice_channel.permissions_for(voice_channel.guild.me).manage_channels:
            with suppress(
                discord.NotFound
            ):  # Sometimes this happens when the user manually deletes their channel
                await voice_channel.delete(reason="AutoVoice: Channel empty.")
                return True
        return False

    async def _generate_channel_name(
        self,
        autovoice_source_config: dict,
        member: discord.Member,
        taken_channel_names: list,
    ) -> str:
        """Return a channel name with an incrementing number appended to it, based on a formatting string."""
        template = None
        if autovoice_source_config["channel_name_type"] in channel_name_template:
            template = channel_name_template[
                autovoice_source_config["channel_name_type"]
            ]
        elif autovoice_source_config["channel_name_type"] == "custom":
            template = autovoice_source_config["channel_name_format"]
        template = template or channel_name_template["username"]

        data = self.get_template_data(member)
        data["random_seed"] = (
            f"{member.id}{random.random()}"  # noqa: S311 # Doesn't need to be secure
        )
        new_channel_name = None
        attempt = 1
        with suppress(Exception):
            new_channel_name = await self.format_template_room_name(
                template, data, attempt
            )

        if not new_channel_name:
            # Either the user screwed with the template, or the template returned nothing. Use a default one instead.
            template = channel_name_template["username"]
            new_channel_name = await self.format_template_room_name(
                template, data, attempt
            )

        # Check for duplicate names
        attempted_channel_names = []
        while (
            new_channel_name in taken_channel_names
            and new_channel_name not in attempted_channel_names
        ):
            attempt += 1
            attempted_channel_names.append(new_channel_name)
            new_channel_name = await self.format_template_room_name(
                template, data, attempt
            )
        return new_channel_name

    #
    # Public methods
    #

    @staticmethod
    def get_template_data(member: discord.Member | discord.User) -> dict[str, Any]:
        """Return a dict of template data based on a member."""
        data = {
            "username": member.display_name,
            "mention": member.mention,
            "datetime": datetime.now(tz=UTC),
            "member": {
                "display_name": member.display_name,
                "mention": member.mention,
                "name": member.name,
                "id": member.id,
                "global_name": member.global_name,
                "bot": member.bot,
                "system": member.system,
            },
            "game": None,
        }
        if isinstance(member, discord.Member):
            for activity in member.activities:
                if activity.type == discord.ActivityType.playing and activity.name:
                    data["game"] = activity.name
                    break
        return data

    async def format_template_room_name(
        self, template: str, data: dict, num: int = 1
    ) -> str:
        """Return a formatted channel name, taking into account the 100 character channel name limit."""
        nums = {"dupenum": num}
        msg = await self.template.render(template, {**data, **nums})
        return msg[:100].strip()

    async def is_admin_or_admin_role(self, who: discord.Role | discord.Member) -> bool:
        """Check if a member (or role) is an admin (role).

        Also takes into account if the setting is enabled.
        """
        if await self.config.guild(who.guild).admin_access():
            if isinstance(who, discord.Role):
                return who in await self.bot.get_admin_roles(who.guild)
            if isinstance(who, discord.Member):
                return await self.bot.is_admin(who)
        return False

    async def is_mod_or_mod_role(self, who: discord.Role | discord.Member) -> bool:
        """Check if a member (or role) is a mod (role).

        Also takes into account if the setting is enabled.
        """
        if await self.config.guild(who.guild).mod_access():
            if isinstance(who, discord.Role):
                return who in await self.bot.get_mod_roles(who.guild)
            if isinstance(who, discord.Member):
                return await self.bot.is_mod(who)
        return False

    def check_perms_source_dest(
        self,
        autovoice_source: discord.VoiceChannel,
        category_dest: discord.CategoryChannel,
        *,
        with_manage_roles_guild: bool = False,
        with_optional_clone_perms: bool = False,
        detailed: bool = False,
    ) -> tuple[bool, bool, str | None]:
        """Check if the permissions in an AutoVoice Source and a destination category are sufficient."""
        source = autovoice_source.permissions_for(autovoice_source.guild.me)
        dest = category_dest.permissions_for(category_dest.guild.me)
        result_required = True
        result_optional = True
        # Required
        for perm_name in self.perms_bot_source:
            result_required = result_required and bool(getattr(source, perm_name))
        for perm_name in self.perms_bot_dest:
            result_required = result_required and bool(getattr(dest, perm_name))
        if with_manage_roles_guild:
            result_required = (
                result_required
                and category_dest.guild.me.guild_permissions.manage_roles
            )
        # Optional
        clone_section = None
        if with_optional_clone_perms:
            clone_result, clone_section = self._check_perms_source_dest_optional(
                autovoice_source, dest, detailed=detailed
            )
            result_optional = result_optional and clone_result
        result = result_required and result_optional
        if not detailed:
            return result_required, result_optional, None

        source_section = SettingDisplay("Required on Source Voice Channel")
        for perm_name in self.perms_bot_source:
            source_section.add(
                perm_name.capitalize().replace("_", " "), getattr(source, perm_name)
            )

        dest_section = SettingDisplay("Required on Destination Category")
        for perm_name in self.perms_bot_dest:
            dest_section.add(
                perm_name.capitalize().replace("_", " "), getattr(dest, perm_name)
            )
        autovoice_sections = [dest_section]

        if with_manage_roles_guild:
            guild_section = SettingDisplay("Required in Guild")
            guild_section.add(
                "Manage roles", category_dest.guild.me.guild_permissions.manage_roles
            )
            autovoice_sections.append(guild_section)

        if clone_section:
            autovoice_sections.append(clone_section)

        status_emoji = "\N{NO ENTRY SIGN}"
        if result:
            status_emoji = "\N{WHITE HEAVY CHECK MARK}"
        elif result_required:
            status_emoji = "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"
        result_str = (
            f"\n{status_emoji} Source VC: {autovoice_source.mention} -> Dest Category: {category_dest.mention}"
            "\n"
            f"{source_section.display(*autovoice_sections)}"
        )
        return result_required, result_optional, result_str

    @staticmethod
    def _check_perms_source_dest_optional(
        autovoice_source: discord.VoiceChannel,
        dest_perms: discord.Permissions,
        *,
        detailed: bool = False,
    ) -> tuple[bool, SettingDisplay | None]:
        result = True
        checked_perms = {}
        source_overwrites = autovoice_source.overwrites or {}
        for permissions in source_overwrites.values():
            # We can't put manage_roles in overwrites, so just get rid of it
            # Also get rid of view_channel, connect, and send_messages, as we will be controlling those
            permissions.update(
                connect=None, manage_roles=None, view_channel=None, send_messages=None
            )
            # Check each permission for each overwrite target to make sure the bot has it allowed in the dest category
            for name, value in permissions:
                if value is not None and name not in checked_perms:
                    check_result = bool(getattr(dest_perms, name))
                    if not detailed and not check_result:
                        return False, None
                    checked_perms[name] = check_result
                    result = result and check_result
        if not detailed:
            return True, None
        clone_section = SettingDisplay(
            "Optional on Destination Category (for source clone)"
        )
        if checked_perms:
            for name, value in checked_perms.items():
                clone_section.add(name.capitalize().replace("_", " "), value)
            return result, clone_section
        return result, None

    async def get_all_autovoice_source_configs(
        self, guild: discord.Guild
    ) -> dict[int, dict[str, Any]]:
        """Return a dict of all AutoVoice source configs, cleaning up any invalid ones."""
        unsorted_list_of_configs = []
        configs = await self.config.custom(
            "AUTOVOICE_SOURCE", str(guild.id)
        ).all()  # Does NOT return default values
        for channel_id in configs:
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.VoiceChannel):
                continue
            config = await self.get_autovoice_source_config(channel)
            if config:
                unsorted_list_of_configs.append((channel.position, channel_id, config))
            else:
                await self.config.custom(
                    "AUTOVOICE_SOURCE", str(guild.id), channel_id
                ).clear()
        result = {}
        for _, channel_id, config in sorted(
            unsorted_list_of_configs, key=lambda source_config: source_config[0]
        ):
            result[int(channel_id)] = config
        return result

    async def get_autovoice_source_config(
        self, autovoice_source: discord.VoiceChannel | discord.abc.GuildChannel | None
    ) -> dict[str, Any] | None:
        """Return the config for an AutoVoice source, or None if not set up yet."""
        if not autovoice_source:
            return None
        if not isinstance(autovoice_source, discord.VoiceChannel):
            return None
        config = await self.config.custom(
            "AUTOVOICE_SOURCE", str(autovoice_source.guild.id), str(autovoice_source.id)
        ).all()  # Returns default values
        if not config["dest_category_id"]:
            return None

        perms = {
            "allow": {
                "view_channel": True,
                "connect": True,
                "send_messages": config["perm_send_messages"],
            },
            "lock": {"view_channel": True, "connect": False, "send_messages": False},
            "deny": {"view_channel": False, "connect": False, "send_messages": False},
        }
        perms["owner"] = {
            **perms["allow"],
            "manage_channels": True if config["perm_owner_manage_channels"] else None,
            "manage_messages": True,
        }
        if config["room_type"] == "private":
            perms["access"] = perms["deny"]
        elif config["room_type"] == "locked":
            perms["access"] = perms["lock"]
        else:
            perms["access"] = perms["allow"]

        config["perms"] = perms
        return config

    async def get_autovoice_info(
        self, autovoice: discord.VoiceChannel | None
    ) -> dict[str, Any] | None:
        """Get info for an AutoVoice room, or None if the voice channel isn't one."""
        if not autovoice:
            return None
        if not await self.config.channel(autovoice).source_channel():
            return None
        return await self.config.channel(autovoice).all()

    @staticmethod
    def check_if_member_or_role_allowed(
        channel: discord.VoiceChannel,
        member_or_role: discord.Member | discord.Role,
    ) -> bool:
        """Check if a member/role is allowed to connect to a voice channel.

        Doesn't matter if they can't see it, it ONLY checks the connect permission.
        Mostly copied from https://github.com/Rapptz/discord.py/blob/master/discord/abc.py:GuildChannel.permissions_for()
        I needed the logic except the "if not base.read_messages:" part that removed all permissions.
        """
        if channel.guild.owner_id == member_or_role.id:
            return True

        default_role = channel.guild.default_role
        base = discord.Permissions(default_role.permissions.value)

        # Handle the role case first
        if isinstance(member_or_role, discord.Role):
            base.value |= member_or_role.permissions.value

            if base.administrator:
                return True

            # Apply @everyone allow/deny first since it's special
            with suppress(KeyError):
                default_allow, default_deny = channel.overwrites[default_role].pair()
                base.handle_overwrite(
                    allow=default_allow.value, deny=default_deny.value
                )

            if member_or_role.is_default():
                return base.connect

            with suppress(KeyError):
                role_allow, role_deny = channel.overwrites[member_or_role].pair()
                base.handle_overwrite(allow=role_allow.value, deny=role_deny.value)

            return base.connect

        member_roles = member_or_role.roles

        # Apply guild roles that the member has.
        for role in member_roles:
            base.value |= role.permissions.value

        # Guild-wide Administrator -> True for everything
        # Bypass all channel-specific overrides
        if base.administrator:
            return True

        # Apply @everyone allow/deny first since it's special
        with suppress(KeyError):
            default_allow, default_deny = channel.overwrites[default_role].pair()
            base.handle_overwrite(allow=default_allow.value, deny=default_deny.value)

        allows = 0
        denies = 0

        # Apply channel specific role permission overwrites
        for role, overwrite in channel.overwrites.items():
            if (
                isinstance(role, discord.Role)
                and role != default_role
                and role in member_roles
            ):
                allows |= overwrite.pair()[0].value
                denies |= overwrite.pair()[1].value

        base.handle_overwrite(allow=allows, deny=denies)

        # Apply member specific permission overwrites
        with suppress(KeyError):
            member_allow, member_deny = channel.overwrites[member_or_role].pair()
            base.handle_overwrite(allow=member_allow.value, deny=member_deny.value)

        if member_or_role.is_timed_out():
            # Timeout leads to every permission except VIEW_CHANNEL and READ_MESSAGE_HISTORY
            # being explicitly denied
            return False

        return base.connect

    def get_member_roles(
        self, autovoice_source: discord.VoiceChannel
    ) -> list[discord.Role]:
        """Get member roles set on an AutoVoice Source."""
        member_roles = []
        # If @everyone is allowed to connect to the source channel, there are no member roles
        if not self.check_if_member_or_role_allowed(
            autovoice_source,
            autovoice_source.guild.default_role,
        ):
            # If it isn't allowed, then member roles are being used
            member_roles.extend(
                role
                for role, overwrite in autovoice_source.overwrites.items()
                if isinstance(role, discord.Role)
                and role != autovoice_source.guild.default_role
                and overwrite.pair()[0].connect
            )
        return member_roles

    async def get_bot_roles(self, guild: discord.Guild) -> list[discord.Role]:
        """Get the additional bot roles that are added to each AutoVoice room."""
        bot_roles = []
        bot_role_ids = []
        some_roles_were_not_found = False
        for bot_role_id in await self.config.guild(guild).bot_access():
            bot_role = guild.get_role(bot_role_id)
            if bot_role:
                bot_roles.append(bot_role)
                bot_role_ids.append(bot_role_id)
            else:
                some_roles_were_not_found = True
        if some_roles_were_not_found:
            # Update the bot role list to remove nonexistent roles
            await self.config.guild(guild).bot_access.set(bot_role_ids)
        return bot_roles
