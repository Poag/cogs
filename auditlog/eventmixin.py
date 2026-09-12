"""AuditLogEventMixin: every discord.py event listener for the auditlog cog.

This is a from-scratch reimplementation of TrustyJAID's ExtendedModLog
`EventMixin` (eventmixin.py). The gate sequence used by (almost) every
listener below is:

1. resolve the guild from the event
2. `guild.id in self.settings` - a guild with no cache entry is never
   logged for any event, this is the primary on/off switch (see
   `AuditLog._ensure_guild` in auditlog.py)
3. `await self.bot.cog_disabled_in_guild(self, guild)`
4. `guild.me.is_timed_out()`
5. the event's own `enabled` flag
6. a `bots` filter, for the handful of events that have one
7. resolve a destination channel via `self.modlog_channel(...)`,
   returning early on `RuntimeError` (no modlog channel configured/usable)
8. ignored-channel / ignored-user checks
9. build the embed or plain-text message
10. an audit log lookup for perpetrator/reason, where applicable - bail out
    if the perpetrator is on the ignored-mods list
11. send, via `self._safe_send` (wraps `channel.send` in
    `try/except discord.HTTPException` so one bad send can't crash the
    listener - see house fix list in the cog's docstring)

Everything here reads/writes `self.settings`/`self.config`/helper methods
that live on the composed `AuditLog` cog class in auditlog.py (shared
helpers, the settings cache, `save()`, `is_ignored_*`, `get_event_colour`,
`modlog_channel`, `get_audit_log_entry`, permission-diff helpers, and the
invite link helpers) - this mixin only holds listeners.
"""

import asyncio
import datetime
import logging
from collections import deque
from enum import Enum
from typing import Optional, Sequence, cast

import discord
from redbot.core import commands
from redbot.core.utils.chat_formatting import box, humanize_list, humanize_timedelta, inline, pagify

log = logging.getLogger("red.auditlog")


class MemberUpdateEnum(Enum):
    """Maps `user_change` Config sub-keys to the `discord.Member` attribute they track."""

    nicknames = "nick"
    roles = "roles"
    pending = "pending"
    timeout = "timed_out_until"
    avatar = "guild_avatar"
    flags = "flags"
    premium_since = "premium_since"

    @staticmethod
    def names():
        return {
            MemberUpdateEnum.nicknames: "Nickname",
            MemberUpdateEnum.roles: "Roles",
            MemberUpdateEnum.pending: "Pending",
            MemberUpdateEnum.timeout: "Timeout until",
            MemberUpdateEnum.avatar: "Guild Avatar",
            MemberUpdateEnum.flags: "Flags",
            MemberUpdateEnum.premium_since: "Premium Since",
        }

    def get_name(self) -> str:
        return self.names().get(self, "Unknown")


class AuditLogEventMixin:
    """Every discord.py listener the auditlog cog reacts to."""

    @commands.Cog.listener()
    async def on_command(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if not self.settings[guild.id]["commands_used"]["enabled"]:
            return
        if isinstance(ctx.channel, (discord.DMChannel, discord.GroupChannel)):
            return
        if await self.is_ignored_channel(guild, ctx.channel):
            return
        if await self.is_ignored_user(guild, ctx.author):
            return
        if await self.is_ignored_mod(guild, ctx.author):
            return
        if guild.me.is_timed_out():
            return
        try:
            channel = await self.modlog_channel(guild, "commands_used")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["commands_used"]["embed"]
        )
        time = ctx.message.created_at
        message = ctx.message
        try:
            can_run = await ctx.command.can_run(ctx, check_all_parents=True)
            can_see = await ctx.command.can_see(ctx)
        except Exception:
            can_run = can_see = "Unknown"
        can_x = f"**See:** {can_see}\n**Run:** {can_run}"
        if ctx.interaction:
            data = ctx.interaction.data or {}
            com_id = data.get("id")
            root_command = data.get("name")
            sub_commands = ""
            arguments = ""
            for option in data.get("options", []):
                if option["type"] in (1, 2):
                    sub_commands += " " + option["name"]
                else:
                    arguments += f"{option['name']}: {option.get('value')}"
                for sub_option in option.get("options", []):
                    if sub_option["type"] in (1, 2):
                        sub_commands += " " + sub_option["name"]
                    else:
                        arguments += f"{sub_option.get('name')}: {sub_option.get('value')}"
                    for arg in sub_option.get("options", []):
                        arguments += f"{arg.get('name')}: {arg.get('value')} "
            command_name = f"{root_command}{sub_commands}"
            com_str = f"</{command_name}:{com_id}> {arguments}"
        else:
            com_str = ctx.message.content
        try:
            com = ctx.command
            privs = com.requires.privilege_level
            user_perms = com.requires.user_perms or discord.Permissions.none()
            # If a subcommand requires only a privilege-level check but its parent
            # checks permissions, we could miss the command's real requirements -
            # so OR every parent's requirements in too.
            my_perms = com.requires.bot_perms
            for p in com.parents:
                if p.requires.privilege_level is not None:
                    if privs is commands.PrivilegeLevel.NONE and p.requires.privilege_level > privs:
                        privs = p.requires.privilege_level
                if p.requires.user_perms:
                    user_perms |= p.requires.user_perms
                if p.requires.bot_perms:
                    my_perms |= p.requires.bot_perms
        except Exception:
            log.exception("Something went wrong figuring out user/bot privileges on a command.")
            return
        if privs is None:
            privs = commands.PrivilegeLevel.NONE
        if privs.name not in self.settings[guild.id]["commands_used"]["privs"]:
            return

        if privs is commands.PrivilegeLevel.MOD:
            mod_role_list = await ctx.bot.get_mod_roles(guild)
            role = (
                humanize_list([r.mention for r in mod_role_list]) + f"\n{privs.name}\n"
                if mod_role_list
                else f"Not Set\n{privs.name}\n"
            )
        elif privs is commands.PrivilegeLevel.ADMIN:
            admin_role_list = await ctx.bot.get_admin_roles(guild)
            role = (
                humanize_list([r.mention for r in admin_role_list]) + f"\n{privs.name}\n"
                if admin_role_list
                else f"Not Set\n{privs.name}\n"
            )
        elif privs is commands.PrivilegeLevel.BOT_OWNER:
            role = humanize_list([f"<@{_id}>" for _id in ctx.bot.owner_ids or []])
            role += f"\n{privs.name}\n"
        elif privs is commands.PrivilegeLevel.GUILD_OWNER:
            role = (guild.owner.mention if guild.owner else "Unknown Server Owner") + f"\n{privs.name}\n"
        else:
            role = f"everyone\n{privs.name}\n"
        if user_perms:
            role += ", ".join(name.replace("_", " ").title() for name, value in user_perms if value)
        i_require = None
        if my_perms:
            i_require = ", ".join(name.replace("_", " ").title() for name, value in my_perms if value)

        if embed_links:
            embed = discord.Embed(
                description=f">>> {com_str}",
                colour=await self.get_event_colour(guild, "commands_used"),
                timestamp=time,
            )
            embed.add_field(name="Channel", value=ctx.channel.mention)
            embed.add_field(name="Author", value=message.author.mention)
            embed.add_field(name="Can", value=str(can_x))
            embed.add_field(name="Requires", value=role)
            if i_require:
                embed.add_field(name="Bot Requires", value=i_require)
            embed.set_author(
                name=f"{message.author} ({message.author.id}) Used a Command",
                icon_url=message.author.display_avatar,
            )
            embed.add_field(name="Member ID", value=box(str(message.author.id)))
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            infomessage = (
                f"{self.settings[guild.id]['commands_used']['emoji']} "
                f"{message.created_at.strftime('%H:%M:%S')} {message.author}"
                f"(`{message.author.id}`) used the following command in {ctx.channel.mention}\n"
                f"> {com_str}"
            )
            await self._safe_send(channel, infomessage[:2000], allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener(name="on_raw_message_delete")
    async def on_raw_message_delete_listener(
        self, payload: discord.RawMessageDeleteEvent, *, check_audit_log: bool = True
    ) -> None:
        guild_id = payload.guild_id
        if guild_id is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        settings = self.settings[guild.id]["message_delete"]
        if not settings["enabled"]:
            return
        channel_id = payload.channel_id
        try:
            channel = await self.modlog_channel(guild, "message_delete")
        except RuntimeError:
            return
        message_channel = guild.get_channel_or_thread(channel_id)
        if message_channel is None:
            return
        if await self.is_ignored_channel(guild, message_channel):
            return
        embed_links = channel.permissions_for(guild.me).embed_links and settings["embed"]
        message = payload.cached_message
        if message is None:
            if settings["cached_only"]:
                return
            if embed_links:
                embed = discord.Embed(
                    description="*Message's content unknown.*",
                    colour=await self.get_event_colour(guild, "message_delete"),
                )
                embed.add_field(name="Channel", value=message_channel.mention)
                embed.set_author(name="Deleted Message")
                embed.add_field(name="Message ID", value=box(str(payload.message_id)))
                await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
            else:
                infomessage = (
                    f"{settings['emoji']} "
                    f"{datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')} "
                    f"A message ({box(str(payload.message_id))}) was deleted in {message_channel.mention}"
                )
                await self._safe_send(
                    channel,
                    f"{infomessage}\n> *Message's content unknown.*",
                    allowed_mentions=self.allowed_mentions,
                )
            return
        await self._cached_message_delete(
            message, guild, settings, channel, check_audit_log=check_audit_log
        )

    async def _cached_message_delete(
        self,
        message: discord.Message,
        guild: discord.Guild,
        settings: dict,
        channel: discord.TextChannel,
        *,
        check_audit_log: bool = True,
    ) -> None:
        if message.author.bot and not settings["bots"]:
            return
        if await self.is_ignored_user(guild, message.author):
            return

        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["message_delete"]["embed"]
        )
        ctx = await self.bot.get_context(message)
        if ctx.valid and self.settings[guild.id]["message_delete"]["ignore_commands"]:
            return
        time = message.created_at
        perp = None
        reason = None
        if channel.permissions_for(guild.me).view_audit_log and check_audit_log:
            action = discord.AuditLogAction.message_delete
            entry = await self.get_audit_log_entry(
                guild, message.author, action, channel_id=message.channel.id
            )
            perp = getattr(entry, "user", None)
            reason = getattr(entry, "reason", None)
            if perp is not None:
                if await self.is_ignored_mod(guild, perp):
                    return

        replying = ""
        if message.reference and message.reference.resolved is not None:
            if isinstance(message.reference.resolved, discord.Message):
                ref_author = message.reference.resolved.author
                ref_jump = message.reference.resolved.jump_url
                replying = f"[{ref_author}]({ref_jump})"
            elif isinstance(message.reference.resolved, discord.DeletedReferencedMessage):
                ref_guild = message.reference.resolved.guild_id
                ref_chan = message.reference.resolved.channel_id
                ref_msg = message.reference.resolved.id
                replying = f"https://discord.com/channels/{ref_guild}/{ref_chan}/{ref_msg}"
        message_channel = cast(discord.TextChannel, message.channel)
        author = message.author
        if perp is None:
            infomessage = (
                f"{settings['emoji']} {discord.utils.format_dt(time)} A message from "
                f"**{author}** (`{author.id}`) was deleted in {message_channel.mention}"
            )
        else:
            infomessage = (
                f"{settings['emoji']} {discord.utils.format_dt(time)} {perp} deleted a message "
                f"from **{author}** (`{author.id}`) in {message_channel.mention}"
            )
        if embed_links:
            content = f">>> {message.content}" if message.content else None
            embed = discord.Embed(
                description=content,
                colour=await self.get_event_colour(guild, "message_delete"),
                timestamp=time,
            )
            embed.add_field(name="Channel", value=message_channel.mention)
            embed.add_field(name="Author", value=message.author.mention)
            if perp:
                embed.add_field(name="Deleted by", value=perp.mention)
            if reason:
                embed.add_field(name="Reason", value=reason)
            if message.attachments:
                files = "\n".join(f"- {inline(a.filename)}" for a in message.attachments)
                embed.add_field(name="Attachments", value=files[:1024])
            if message.stickers:
                files = ""
                for sticker in message.stickers:
                    if sticker.format is discord.StickerFormatType.lottie:
                        files += f"- {inline(sticker.name)}\n"
                    else:
                        files += f"- [{inline(sticker.name)}]({sticker.url})"
                embed.add_field(name="Stickers", value=files[:1024])
            if getattr(message, "poll", None) is not None:
                poll = message.poll
                poll_info = f"Poll Question: {poll.question}\n"
                for answer in poll.answers:
                    poll_info += f"- {answer.text}\n"
                embed.description = poll_info
            if getattr(message, "message_snapshots", None):
                if len(message.message_snapshots) == 1:
                    ss = message.message_snapshots[0]
                    if ss.content:
                        embed.description = ss.content
                    elif ss.embeds:
                        embed.description = "\n".join(
                            f"{e.title}\n{e.description}" for e in ss.embeds
                        )[:4096]
                    embed.add_field(name="Forwarded Message", value="True")
            if replying:
                embed.add_field(name="Replying to:", value=replying)
            embed.add_field(name="Message ID", value=box(str(message.id)))
            embed.set_author(
                name=f"{author} ({author.id}) - Deleted Message",
                icon_url=message.author.display_avatar,
            )
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            clean_msg = message.clean_content[: (1990 - len(infomessage))]
            await self._safe_send(
                channel, f"{infomessage}\n>>> {clean_msg}", allowed_mentions=self.allowed_mentions
            )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        guild_id = payload.guild_id
        if guild_id is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        settings = self.settings[guild.id]["message_delete"]
        if not settings["enabled"] or not settings["bulk_enabled"]:
            return
        channel_id = payload.channel_id
        message_channel = guild.get_channel_or_thread(channel_id)
        if message_channel is None:
            return
        try:
            channel = await self.modlog_channel(guild, "message_delete")
        except RuntimeError:
            return
        if await self.is_ignored_channel(guild, message_channel):
            return
        embed_links = channel.permissions_for(guild.me).embed_links and settings["embed"]
        message_amount = len(payload.message_ids)
        if embed_links:
            embed = discord.Embed(
                description=message_channel.mention,
                colour=await self.get_event_colour(guild, "message_delete"),
            )
            embed.set_author(name="Bulk message delete", icon_url=guild.icon)
            embed.add_field(name="Channel", value=message_channel.mention)
            embed.add_field(name="Messages deleted", value=str(message_amount))
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            infomessage = (
                f"{settings['emoji']} "
                f"{datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')} "
                f"Bulk message delete in {message_channel.mention}, {message_amount} messages deleted."
            )
            await self._safe_send(channel, infomessage, allowed_mentions=self.allowed_mentions)
        if settings["bulk_individual"]:
            # Replay each cached message through the single-delete embed builder, but
            # with the audit log lookup disabled - a bulk delete's perpetrator can't be
            # meaningfully attributed to any one message in it.
            for message in payload.cached_messages:
                new_payload = discord.RawMessageDeleteEvent(
                    {"id": message.id, "channel_id": channel_id, "guild_id": guild_id}
                )
                new_payload.cached_message = message
                try:
                    await self.on_raw_message_delete_listener(new_payload, check_audit_log=False)
                except Exception:
                    log.exception("Error replaying a bulk-deleted message for guild %s", guild_id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        if guild.id not in self.settings:
            return
        if not self.settings[guild.id]["user_join"]["enabled"]:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if await self.is_ignored_user(guild, member):
            return
        if guild.me.is_timed_out():
            return
        try:
            channel = await self.modlog_channel(guild, "user_join")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["user_join"]["embed"]
        )
        users = len(guild.members)
        user_created = int(member.created_at.timestamp())
        created_on = f"<t:{user_created}>\n(<t:{user_created}:R>)"
        possible_link = await self.get_invite_link(member)
        if embed_links:
            embed = discord.Embed(
                description=str(member),
                colour=await self.get_event_colour(guild, "user_join"),
                timestamp=member.joined_at or datetime.datetime.now(datetime.timezone.utc),
            )
            embed.add_field(name="Member", value=member.mention)
            embed.add_field(name="Member ID", value=box(str(member.id)))
            embed.add_field(name="Total Users:", value=str(users))
            embed.add_field(name="Account created on:", value=created_on)
            embed.set_author(
                name=f"{member} ({member.id}) has joined the guild",
                url=member.display_avatar,
                icon_url=member.display_avatar,
            )
            if possible_link:
                embed.add_field(name="Invite Link", value=possible_link)
            embed.set_thumbnail(url=member.display_avatar)
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            time = datetime.datetime.now(datetime.timezone.utc)
            msg = (
                f"{self.settings[guild.id]['user_join']['emoji']} {discord.utils.format_dt(time)} "
                f"**{member}**(`{member.id}`) joined the guild. Total members: {users}"
            )
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, member: discord.Member) -> None:
        """Only used to remember that a member was banned rather than kicked/left."""
        self._ban_cache.setdefault(guild.id, []).append(member.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        await asyncio.sleep(5)
        if guild.id in self._ban_cache and member.id in self._ban_cache[guild.id]:
            # This was a ban, which on_member_ban already tracks separately.
            return
        if guild.id not in self.settings:
            return
        if not self.settings[guild.id]["user_left"]["enabled"]:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if await self.is_ignored_user(guild, member):
            return
        try:
            channel = await self.modlog_channel(guild, "user_left")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["user_left"]["embed"]
        )
        time = datetime.datetime.now(datetime.timezone.utc)
        entry = await self.get_audit_log_entry(guild, member, discord.AuditLogAction.kick)
        joined = member.joined_at
        member_time = None
        if joined is not None:
            member_time = f"{discord.utils.format_dt(joined, 'D')} ({discord.utils.format_dt(joined, 'R')})"

        perp = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)
        if embed_links:
            embed = discord.Embed(
                description=str(member),
                colour=await self.get_event_colour(guild, "user_left"),
                timestamp=time,
            )
            embed.add_field(name="Member", value=member.mention)
            embed.add_field(name="Member ID", value=box(str(member.id)))
            embed.add_field(name="Total Users:", value=str(len(guild.members)))
            if member_time is not None:
                embed.add_field(name="Member since:", value=member_time)
            if perp:
                embed.add_field(name="Kicked", value=perp.mention)
            if reason:
                embed.add_field(name="Reason", value=str(reason), inline=False)
            embed.set_author(
                name=f"{member} ({member.id}) has left the guild",
                url=member.display_avatar,
                icon_url=member.display_avatar,
            )
            embed.set_thumbnail(url=member.display_avatar)
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            msg = (
                f"{self.settings[guild.id]['user_left']['emoji']} {discord.utils.format_dt(time)} "
                f"**{member}**(`{member.id}`) left the guild. Total members: {len(guild.members)}"
            )
            if perp:
                if await self.is_ignored_mod(guild, perp):
                    return
                msg = (
                    f"{self.settings[guild.id]['user_left']['emoji']} {discord.utils.format_dt(time)} "
                    f"**{member}**(`{member.id}`) was kicked by {perp}. "
                    f"Total members: {len(guild.members)}"
                )
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, new_channel: discord.abc.GuildChannel) -> None:
        guild = new_channel.guild
        if guild.id not in self.settings:
            return
        if not self.settings[guild.id]["channel_create"]["enabled"]:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if await self.is_ignored_channel(guild, new_channel):
            return
        try:
            channel = await self.modlog_channel(guild, "channel_create")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["channel_create"]["embed"]
        )
        time = datetime.datetime.now(datetime.timezone.utc)
        channel_type = str(new_channel.type).replace("_", " ").title()
        embed = discord.Embed(
            description=new_channel.name,
            timestamp=time,
            colour=await self.get_event_colour(guild, "channel_create"),
        )
        embed.set_author(name=f"{channel_type} Channel Created ({new_channel.id})")
        entry = await self.get_audit_log_entry(
            guild, new_channel, discord.AuditLogAction.channel_create
        )
        perp = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)

        perp_msg = ""
        embed.add_field(name="Channel", value=new_channel.mention)
        embed.add_field(name="Type", value=channel_type)
        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
            perp_msg = f"by {perp} (`{perp.id}`)"
            embed.add_field(name="Created by ", value=perp.mention)
        if reason:
            perp_msg += f" Reason: {reason}"
            embed.add_field(name="Reason ", value=reason, inline=False)
        embed.add_field(name="Channel ID", value=box(str(new_channel.id)))
        msg = (
            f"{self.settings[guild.id]['channel_create']['emoji']} {discord.utils.format_dt(time)} "
            f"{channel_type} channel created {perp_msg} {new_channel.mention}"
        )
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, old_channel: discord.abc.GuildChannel) -> None:
        guild = old_channel.guild
        if guild.id not in self.settings:
            return
        if not self.settings[guild.id]["channel_delete"]["enabled"]:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if await self.is_ignored_channel(guild, old_channel):
            return
        try:
            channel = await self.modlog_channel(guild, "channel_delete")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["channel_delete"]["embed"]
        )
        channel_type = str(old_channel.type).replace("_", " ").title()
        time = datetime.datetime.now(datetime.timezone.utc)
        embed = discord.Embed(
            description=old_channel.name,
            timestamp=time,
            colour=await self.get_event_colour(guild, "channel_delete"),
        )
        embed.set_author(name=f"{channel_type} Channel Deleted ({old_channel.id})")
        entry = await self.get_audit_log_entry(
            guild, old_channel, discord.AuditLogAction.channel_delete
        )
        perp = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)

        perp_msg = ""
        embed.add_field(name="Channel", value=old_channel.mention)
        embed.add_field(name="Type", value=channel_type)
        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
            perp_msg = f"by {perp} (`{perp.id}`)"
            embed.add_field(name="Deleted by ", value=perp.mention)
        if reason:
            perp_msg += f" Reason: {reason}"
            embed.add_field(name="Reason ", value=reason, inline=False)
        embed.add_field(name="Channel ID", value=box(str(old_channel.id)))
        msg = (
            f"{self.settings[guild.id]['channel_delete']['emoji']} {discord.utils.format_dt(time)} "
            f"{channel_type} channel deleted {perp_msg} #{old_channel.name} ({old_channel.id})"
        )
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        """Cache recent audit log entries so `get_audit_log_entry` can match without a fetch."""
        if entry.guild.id not in self.audit_log:
            self.audit_log[entry.guild.id] = deque(maxlen=10)
        self.audit_log[entry.guild.id].append(entry)

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        guild = before.guild
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if not self.settings[guild.id]["channel_change"]["enabled"]:
            return
        if await self.is_ignored_channel(guild, before):
            return
        try:
            channel = await self.modlog_channel(guild, "channel_change")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["channel_change"]["embed"]
        )
        channel_type = str(after.type).replace("_", " ").title()
        time = datetime.datetime.now(datetime.timezone.utc)
        embed = discord.Embed(
            description=after.mention,
            timestamp=time,
            colour=await self.get_event_colour(guild, "channel_change"),
        )
        embed.set_author(
            name=f"{channel_type} Channel Updated {before.name} ({before.id})"
        )
        msg = (
            f"{self.settings[guild.id]['channel_change']['emoji']} {discord.utils.format_dt(time)} "
            f"Updated channel {before.name}\n"
        )
        worth_updating = False
        perp = None
        reason = None
        channel_updates = {
            "name": "Name:",
            "topic": "Topic:",
            "category": "Category:",
            "slowmode_delay": "Slowmode delay:",
            "bitrate": "Bitrate:",
            "user_limit": "User limit:",
        }
        before_text = ""
        after_text = ""
        for attr, name in channel_updates.items():
            before_attr = getattr(before, attr, None)
            after_attr = getattr(after, attr, None)
            if before_attr != after_attr:
                worth_updating = True
                if before_attr == "":
                    before_attr = "None"
                if after_attr == "":
                    after_attr = "None"
                msg += f"Before {name} {before_attr}\n"
                msg += f"After {name} {after_attr}\n"
                before_text += f"- {name} {before_attr}\n"
                after_text += f"- {name} {after_attr}\n"
                entry = await self.get_audit_log_entry(
                    guild, before, discord.AuditLogAction.channel_update
                )
                perp = getattr(entry, "user", None)
                reason = getattr(entry, "reason", None)

        if before.is_nsfw() != after.is_nsfw():
            worth_updating = True
            msg += f"Before NSFW {before.is_nsfw()}\n"
            msg += f"After NSFW {after.is_nsfw()}\n"
            before_text += f"- Age Restricted: {before.is_nsfw()}"
            after_text += f"- Age Restricted: {after.is_nsfw()}"
            entry = await self.get_audit_log_entry(
                guild, before, discord.AuditLogAction.channel_update
            )
            perp = getattr(entry, "user", None)
            reason = getattr(entry, "reason", None)
        if before_text and after_text:
            for page in pagify(before_text, page_length=1024):
                embed.add_field(name="Before", value=page)
            for page in pagify(after_text, page_length=1024):
                embed.add_field(name="After", value=page)
        p_msg = await self.get_permission_change(before, after)
        if p_msg != "":
            worth_updating = True
            msg += "Permissions Changed: " + p_msg
            for page in pagify(p_msg, page_length=1024):
                embed.add_field(name="Permissions", value=page)

        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
            msg += "Updated by " + str(perp) + "\n"
            embed.add_field(name="Updated by ", value=perp.mention)
        if reason:
            msg += "Reason " + reason + "\n"
            embed.add_field(name="Reason ", value=reason, inline=False)
        embed.add_field(name="Channel ID", value=box(str(after.id)))
        if not worth_updating:
            return
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        guild = before.guild
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if not self.settings[guild.id]["role_change"]["enabled"]:
            return
        try:
            channel = await self.modlog_channel(guild, "role_change")
        except RuntimeError:
            return
        entry = await self.get_audit_log_entry(guild, before, discord.AuditLogAction.role_update)
        perp = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)
        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["role_change"]["embed"]
        )
        time = datetime.datetime.now(datetime.timezone.utc)
        # Bug fix (vs. ExtendedModLog): route through get_event_colour instead of
        # hardcoding `after.colour` directly, so a custom `role_change` colour set
        # via `[p]auditlog colour` isn't silently ignored on updates (it already
        # worked correctly for role create/delete). get_event_colour still falls
        # back to the role's own colour when no custom colour is configured.
        embed = discord.Embed(
            description=after.name,
            colour=await self.get_event_colour(guild, "role_change", changed_object=after),
            timestamp=time,
        )
        msg = (
            f"{self.settings[guild.id]['role_change']['emoji']} {discord.utils.format_dt(time)} "
            f"Updated role **{before.name}**\n"
        )
        if after is guild.default_role:
            embed.set_author(name="Updated role ")
        else:
            embed.set_author(name=f"Updated Role ({before.id})")
        embed.add_field(name="Role", value=after.mention)
        if perp:
            msg += "Updated by " + str(perp) + "\n"
            embed.add_field(name="Updated by ", value=perp.mention)
        if reason:
            msg += "Reason " + reason + "\n"
            embed.add_field(name="Reason ", value=reason, inline=False)
        role_updates = {
            "name": "Name:",
            "color": "Colour:",
            "mentionable": "Mentionable:",
            "hoist": "Is Hoisted:",
            "display_icon": "Icon:",
        }
        worth_updating = False
        for attr, name in role_updates.items():
            before_attr = getattr(before, attr)
            after_attr = getattr(after, attr)
            if before_attr != after_attr:
                worth_updating = True
                if before_attr == "":
                    before_attr = "None"
                if after_attr == "":
                    after_attr = "None"
                msg += f"Before {name} {before_attr}\n"
                msg += f"After {name} {after_attr}\n"
                embed.add_field(name="Before " + name, value=str(before_attr))
                embed.add_field(name="After " + name, value=str(after_attr))
        if before.display_icon != after.display_icon:
            if isinstance(before.display_icon, discord.Asset):
                embed.set_image(url=before.display_icon)
        if isinstance(after.display_icon, discord.Asset):
            embed.set_thumbnail(url=after.display_icon)

        p_msg = await self.get_role_permission_change(before, after)
        if p_msg != "":
            worth_updating = True
            msg += "Permissions Changed: " + p_msg
            embed.add_field(name="Permissions", value=p_msg[:1024])
        embed.add_field(name="Role ID", value=box(str(after.id)))
        if not worth_updating:
            return
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        guild = role.guild
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if not self.settings[guild.id]["role_create"]["enabled"]:
            return
        try:
            channel = await self.modlog_channel(guild, "role_create")
        except RuntimeError:
            return
        entry = await self.get_audit_log_entry(guild, role, discord.AuditLogAction.role_create)
        perp = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)
        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["role_create"]["embed"]
        )
        time = datetime.datetime.now(datetime.timezone.utc)
        embed = discord.Embed(
            description=role.name,
            colour=await self.get_event_colour(guild, "role_create"),
            timestamp=time,
        )
        embed.set_author(name=f"Role created ({role.id})")
        msg = (
            f"{self.settings[guild.id]['role_create']['emoji']} {discord.utils.format_dt(time)} "
            f"Role created {role.name}\n"
        )
        embed.add_field(name="Role", value=role.mention)
        if perp:
            embed.add_field(name="Created by", value=perp.mention)
            msg += "By " + str(perp) + "\n"
        if reason:
            msg += "Reason " + reason + "\n"
            embed.add_field(name="Reason ", value=reason, inline=False)
        embed.add_field(name="Role ID", value=box(str(role.id)))
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        guild = role.guild
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if not self.settings[guild.id]["role_delete"]["enabled"]:
            return
        try:
            channel = await self.modlog_channel(guild, "role_delete")
        except RuntimeError:
            return
        entry = await self.get_audit_log_entry(guild, role, discord.AuditLogAction.role_delete)
        perp = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)
        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["role_delete"]["embed"]
        )
        time = datetime.datetime.now(datetime.timezone.utc)
        embed = discord.Embed(
            description=role.name,
            timestamp=time,
            colour=await self.get_event_colour(guild, "role_delete"),
        )
        embed.set_author(name=f"Role deleted ({role.id})")
        msg = (
            f"{self.settings[guild.id]['role_delete']['emoji']} {discord.utils.format_dt(time)} "
            f"Role deleted **{role.name}**\n"
        )
        embed.add_field(name="Role", value=role.mention)
        if perp:
            embed.add_field(name="Deleted by", value=perp.mention)
            msg += "By " + str(perp) + "\n"
        if reason:
            msg += "Reason " + reason + "\n"
            embed.add_field(name="Reason ", value=reason, inline=False)
        embed.add_field(name="Role ID", value=box(str(role.id)))
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        guild = before.guild
        if guild is None:
            return
        if isinstance(before.channel, (discord.DMChannel, discord.GroupChannel)):
            return
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        settings = self.settings[guild.id]["message_edit"]
        if not settings["enabled"]:
            return
        if before.author.bot and not settings["bots"]:
            return
        if before.content == after.content:
            return
        try:
            channel = await self.modlog_channel(guild, "message_edit")
        except RuntimeError:
            return
        if await self.is_ignored_channel(guild, after.channel):
            return
        if await self.is_ignored_user(guild, after.author):
            return
        embed_links = channel.permissions_for(guild.me).embed_links and settings["embed"]
        replying = ""
        if before.reference is not None and before.reference.resolved is not None:
            if isinstance(before.reference.resolved, discord.Message):
                ref_author = before.reference.resolved.author
                ref_jump = before.reference.resolved.jump_url
                replying = f"[{ref_author}]({ref_jump})"
            elif isinstance(before.reference.resolved, discord.DeletedReferencedMessage):
                ref_guild = before.reference.resolved.guild_id
                ref_chan = before.reference.resolved.channel_id
                ref_msg = before.reference.resolved.id
                replying = f"https://discord.com/channels/{ref_guild}/{ref_chan}/{ref_msg}"
        if embed_links:
            embed = discord.Embed(
                description=f">>> {before.content}",
                colour=await self.get_event_colour(guild, "message_edit"),
                timestamp=before.created_at,
            )
            embed.add_field(name="After Edit", value=after.jump_url)
            embed.add_field(name="Channel", value=before.channel.jump_url)
            if replying:
                embed.add_field(name="Replying to:", value=replying)
            embed.add_field(name="Author", value=before.author.mention)
            embed.set_author(
                name=f"{before.author} ({before.author.id}) - Edited Message",
                icon_url=str(before.author.display_avatar),
            )
            embed.add_field(name="Message ID", value=box(str(after.id)))
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            msg = (
                f"{settings['emoji']} {before.created_at.strftime('%H:%M:%S')} "
                f"**{before.author}** (`{before.author.id}`) edited a message in "
                f"{before.channel.mention}.\nBefore:\n> {before.content}\nAfter:\n> {after.jump_url}"
            )
            await self._safe_send(channel, msg[:2000], allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        guild = after
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if not self.settings[guild.id]["guild_change"]["enabled"]:
            return
        try:
            channel = await self.modlog_channel(guild, "guild_change")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["guild_change"]["embed"]
        )
        time = datetime.datetime.now(datetime.timezone.utc)
        embed = discord.Embed(
            timestamp=time, colour=await self.get_event_colour(guild, "guild_change")
        )
        embed.set_author(name="Updated Guild", icon_url=guild.icon)
        embed.set_thumbnail(url=guild.icon)
        msg = (
            f"{self.settings[guild.id]['guild_change']['emoji']} {discord.utils.format_dt(time)} "
            "Guild updated\n"
        )
        guild_updates = {
            "name": "Name:",
            "afk_timeout": "AFK Timeout:",
            "afk_channel": "AFK Channel:",
            "icon": "Server Icon:",
            "owner": "Server Owner:",
            "splash": "Splash Image:",
            "system_channel": "Welcome message channel:",
            "verification_level": "Verification Level:",
        }
        worth_updating = False
        for attr, name in guild_updates.items():
            before_attr = getattr(before, attr)
            after_attr = getattr(after, attr)
            if before_attr != after_attr:
                worth_updating = True
                if attr == "icon":
                    embed.description = "Server Icon Updated"
                    embed.set_image(url=after.icon)
                    continue
                msg += f"Before {name} {before_attr}\n"
                msg += f"After {name} {after_attr}\n"
                embed.add_field(name="Before " + name, value=str(before_attr))
                embed.add_field(name="After " + name, value=str(after_attr))
        if not worth_updating:
            return
        perp = None
        reason = None
        if channel.permissions_for(guild.me).view_audit_log:
            entry = await self.get_audit_log_entry(guild, None, discord.AuditLogAction.guild_update)
            perp = getattr(entry, "user", None)
            reason = getattr(entry, "reason", None)

        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
            embed.add_field(name="Updated by", value=str(perp))
        if reason:
            embed.add_field(name="Reasons ", value=reason, inline=False)
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_guild_emojis_update(
        self, guild: discord.Guild, before: Sequence[discord.Emoji], after: Sequence[discord.Emoji]
    ) -> None:
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if not self.settings[guild.id]["emoji_change"]["enabled"]:
            return
        try:
            channel = await self.modlog_channel(guild, "emoji_change")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["emoji_change"]["embed"]
        )
        time = datetime.datetime.now(datetime.timezone.utc)
        embed = discord.Embed(timestamp=time, colour=await self.get_event_colour(guild, "emoji_change"))
        embed.description = ""
        embed.set_author(name="Updated Server Emojis")
        msg = (
            f"{self.settings[guild.id]['emoji_change']['emoji']} {discord.utils.format_dt(time)} "
            "Updated Server Emojis"
        )
        worth_updating = False
        b = set(before)
        a = set(after)
        added_emoji: Optional[discord.Emoji] = None
        removed_emoji: Optional[discord.Emoji] = None
        try:
            added_emoji = (a - b).pop()
        except KeyError:
            pass
        try:
            removed_emoji = (b - a).pop()
        except KeyError:
            pass
        if added_emoji is not None:
            to_iter = before + (added_emoji,)
        else:
            to_iter = before
        changed_emoji = set((e, e.name, tuple(e.roles)) for e in after)
        changed_emoji.difference_update((e, e.name, tuple(e.roles)) for e in to_iter)
        old_emoji = None
        try:
            changed_emoji = changed_emoji.pop()[0]
        except KeyError:
            changed_emoji = None
        else:
            for old_emoji in before:
                if old_emoji.id == changed_emoji.id:
                    break
            else:
                changed_emoji = None
        action = None
        if removed_emoji is not None:
            worth_updating = True
            new_msg = f"`{removed_emoji}` (ID: {removed_emoji.id}) Removed from the guild\n"
            msg += new_msg
            embed.description += new_msg
            action = discord.AuditLogAction.emoji_delete
        elif added_emoji is not None:
            worth_updating = True
            new_msg = f"{added_emoji} `{added_emoji}` Added to the guild\n"
            msg += new_msg
            embed.description += new_msg
            action = discord.AuditLogAction.emoji_create
        elif changed_emoji is not None:
            worth_updating = True
            emoji_name = f"{changed_emoji} `{changed_emoji}`"
            if old_emoji.name != changed_emoji.name:
                new_msg = f"{emoji_name} Renamed from {old_emoji.name} to {changed_emoji.name}\n"
                action = discord.AuditLogAction.emoji_update
                msg += new_msg
                embed.description += new_msg
            if old_emoji.roles != changed_emoji.roles:
                worth_updating = True
                if not changed_emoji.roles:
                    new_msg = f"{emoji_name} Changed to unrestricted.\n"
                elif not old_emoji.roles:
                    new_msg = "{} Restricted to roles: {}\n".format(
                        emoji_name,
                        humanize_list([f"{role.name} ({role.id})" for role in changed_emoji.roles]),
                    )
                else:
                    new_msg = "{} Role restriction changed from\n {}\n To\n {}".format(
                        emoji_name,
                        humanize_list([f"{role.mention} ({role.id})" for role in old_emoji.roles]),
                        humanize_list([f"{role.name} ({role.id})" for role in changed_emoji.roles]),
                    )
                msg += new_msg
                embed.description += new_msg
        perp = None
        reason = None
        if not worth_updating:
            return
        if channel.permissions_for(guild.me).view_audit_log:
            if action:
                entry = await self.get_audit_log_entry(guild, None, action)
                perp = getattr(entry, "user", None)
                reason = getattr(entry, "reason", None)
        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
            embed.add_field(name="Updated by ", value=perp.mention)
            msg += "Updated by " + str(perp) + "\n"
        if reason:
            msg += "Reason " + reason + "\n"
            embed.add_field(name="Reason ", value=reason, inline=False)
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if not self.settings[guild.id]["voice_change"]["enabled"]:
            return
        if member.bot and not self.settings[guild.id]["voice_change"]["bots"]:
            return
        try:
            channel = await self.modlog_channel(guild, "voice_change")
        except RuntimeError:
            return
        if after.channel is not None and await self.is_ignored_channel(guild, after.channel):
            return
        if before.channel is not None and await self.is_ignored_channel(guild, before.channel):
            return
        if await self.is_ignored_user(guild, member):
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["voice_change"]["embed"]
        )
        time = datetime.datetime.now(datetime.timezone.utc)
        embed = discord.Embed(timestamp=time, colour=await self.get_event_colour(guild, "voice_change"))
        msg = (
            f"{self.settings[guild.id]['voice_change']['emoji']} {discord.utils.format_dt(time)} "
            f"Updated Voice State for **{member}** (`{member.id}`)"
        )
        embed.set_author(name=f"{member} ({member.id}) Voice State Update")
        change_type = None
        worth_updating = False
        if before.deaf != after.deaf:
            worth_updating = True
            change_type = "deaf"
            chan_msg = (
                f"{member.mention} was deafened. " if after.deaf else f"{member.mention} was undeafened. "
            )
            msg += chan_msg + "\n"
            embed.description = chan_msg
        if before.mute != after.mute:
            worth_updating = True
            change_type = "mute"
            chan_msg = (
                f"{member.mention} was muted." if after.mute else f"{member.mention} was unmuted. "
            )
            msg += chan_msg + "\n"
            embed.description = chan_msg
        if before.channel != after.channel:
            worth_updating = True
            change_type = "channel"
            if before.channel is None:
                channel_name = f"`{after.channel.name}` ({after.channel.id}) {after.channel.mention}"
                chan_msg = f"{member.mention} has joined {channel_name}"
            elif after.channel is None:
                channel_name = f"`{before.channel.name}` ({before.channel.id}) {before.channel.mention}"
                chan_msg = f"{member.mention} has left {channel_name}"
            else:
                after_chan = f"`{after.channel.name}` ({after.channel.id}) {after.channel.mention}"
                before_chan = f"`{before.channel.name}` ({before.channel.id}) {before.channel.mention}"
                chan_msg = f"{member.mention} has moved from {before_chan} to {after_chan}"
            msg += chan_msg + "\n"
            embed.description = chan_msg
        if not worth_updating:
            return
        perp = None
        reason = None
        if channel.permissions_for(guild.me).view_audit_log and change_type:
            entry = await self.get_audit_log_entry(
                guild, member, discord.AuditLogAction.member_update, extra=change_type
            )
            if entry and getattr(entry.after, change_type, None) is not None:
                perp = getattr(entry, "user", None)
                reason = getattr(entry, "reason", None)
        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
            embed.add_field(name="Updated by", value=perp.mention)
        if reason:
            msg += "Reason " + reason + "\n"
            embed.add_field(name="Reason ", value=reason, inline=False)
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        guild = before.guild
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if not self.settings[guild.id]["user_change"]["enabled"]:
            return
        if not self.settings[guild.id]["user_change"]["bots"] and after.bot:
            return
        if await self.is_ignored_user(guild, after):
            return
        try:
            channel = await self.modlog_channel(guild, "user_change")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["user_change"]["embed"]
        )
        time = datetime.datetime.now(datetime.timezone.utc)
        embed = discord.Embed(timestamp=time, colour=await self.get_event_colour(guild, "user_change"))
        msg = (
            f"{self.settings[guild.id]['user_change']['emoji']} {discord.utils.format_dt(time)} "
            f"Member updated **{before}** (`{before.id}`)\n"
        )
        embed.description = ""
        embed.set_author(name=f"{before} ({before.id}) updated", icon_url=before.display_avatar)
        perp = None
        reason = None
        worth_sending = False
        before_text = ""
        after_text = ""
        for update_type in MemberUpdateEnum:
            attr = update_type.value
            if not self.settings[guild.id]["user_change"][update_type.name]:
                continue
            before_attr = getattr(before, attr, None)
            after_attr = getattr(after, attr, None)
            if before_attr == after_attr:
                continue
            if attr == "roles":
                b = set(before.roles)
                a = set(after.roles)
                removed_roles = list(b - a)
                added_roles = list(a - b)
                perps = set()
                reasons = set()
                for role in removed_roles:
                    entry = await self.get_audit_log_entry(
                        guild, before, discord.AuditLogAction.member_role_update, removed_roles=[role]
                    )
                    perp = getattr(entry, "user", None)
                    reason = getattr(entry, "reason", None)
                    msg += f"{after.name} had the {role.name} role removed."
                    embed.description += f"{after.mention} had the {role.mention} role removed.\n"
                    if perp:
                        perps.add(perp.mention)
                    if reason:
                        reasons.add(reason)
                    worth_sending = True
                for role in added_roles:
                    entry = await self.get_audit_log_entry(
                        guild, before, discord.AuditLogAction.member_role_update, added_roles=[role]
                    )
                    perp = getattr(entry, "user", None)
                    reason = getattr(entry, "reason", None)
                    msg += f"{after.name} had the {role.name} role applied."
                    embed.description += f"{after.mention} had the {role.mention} role applied.\n"
                    if perp:
                        perps.add(perp.mention)
                    if reason:
                        reasons.add(reason)
                    worth_sending = True
                if perps:
                    msg += "Updated by " + humanize_list(list(perps)) + "."
                    embed.add_field(name="Updated by ", value="\n".join(f"- {p}" for p in perps))
                if reasons:
                    reason_str = "\n".join(f"- {r}" for r in reasons)
                    msg += "Reason: " + reason_str
                    embed.add_field(name="Reason: ", value=reason_str)
            elif attr == "flags":
                # Bug fix (vs. ExtendedModLog): this branch built its embed content but
                # never set worth_sending, so a flags-only change was silently dropped.
                worth_sending = True
                changed_flags = [
                    key.replace("_", " ").title()
                    for key, value in dict(before.flags ^ after.flags).items()
                    if value
                ]
                flags_str = "\n".join(f"- {flag}" for flag in changed_flags)
                add_str = f"{after.mention} had the following flag changes:\n{flags_str}"
                msg += add_str
                embed.description += add_str
            elif attr == "guild_avatar":
                worth_sending = True
                embed.set_image(url=after_attr)
                if after_attr:
                    embed.description += f"- {after.mention} changed their [guild avatar]({after_attr}).\n"
                else:
                    embed.description += f"- {after.mention} removed their guild avatar.\n"
            elif attr == "premium_since":
                worth_sending = True
                if after_attr:
                    since = discord.utils.format_dt(after_attr, "F")
                    relative = discord.utils.format_dt(after_attr, "R")
                    embed.description += (
                        f"- {after.mention} has subscribed to the guild since {since} ({relative})."
                    )
                elif before_attr:
                    since = discord.utils.format_dt(before_attr, "F")
                    relative = discord.utils.format_dt(before_attr, "R")
                    embed.description += (
                        f"- {after.mention} has unsubscribed from the guild. "
                        f"They started {since} ({relative})."
                    )
            else:
                entry = await self.get_audit_log_entry(guild, before, discord.AuditLogAction.member_update)
                perp = getattr(entry, "user", None)
                reason = getattr(entry, "reason", None)
                worth_sending = True
                if isinstance(before_attr, datetime.datetime):
                    before_attr = (
                        f"{discord.utils.format_dt(before_attr)} "
                        f"({discord.utils.format_dt(before_attr, style='R')})"
                    )
                if isinstance(after_attr, datetime.datetime):
                    after_attr = (
                        f"{discord.utils.format_dt(after_attr)} "
                        f"({discord.utils.format_dt(after_attr, style='R')})"
                    )
                msg += f"Before {update_type.get_name()} {before_attr}\n"
                msg += f"After {update_type.get_name()} {after_attr}\n"
                embed.description = f"{after.mention} has updated."
                before_text += f"- {update_type.get_name()}: {before_attr}\n"
                after_text += f"- {update_type.get_name()}: {after_attr}\n"
                if perp:
                    msg += "Updated by " + f"{perp}\n"
                    embed.add_field(name="Updated by ", value=perp.mention)
                if reason:
                    msg += "Reason: " + f"{reason}\n"
                    embed.add_field(name="Reason", value=reason, inline=False)
        if before_text and after_text:
            for page in pagify(before_text, page_length=1024):
                embed.add_field(name="Before", value=page)
            for page in pagify(after_text, page_length=1024):
                embed.add_field(name="After", value=page)
        if not worth_sending:
            return

        embed.add_field(name="Member ID", value=box(str(after.id)))
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        guild = self.bot.get_guild(invite.guild.id)
        if guild is None:
            return
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if invite.code not in self.settings[guild.id]["invite_links"]:
            # This snapshot is kept regardless of whether invite_created's embed is
            # enabled below - it feeds get_invite_link's join attribution and the
            # 5-minute invite_links_loop refresh.
            created_at = getattr(invite, "created_at", datetime.datetime.now(datetime.timezone.utc))
            inviter = getattr(invite, "inviter", discord.Object(id=0))
            invite_channel = getattr(invite, "channel", discord.Object(id=0))
            self.settings[guild.id]["invite_links"][invite.code] = {
                "uses": getattr(invite, "uses", 0),
                "max_age": getattr(invite, "max_age", None),
                "created_at": created_at.timestamp(),
                "max_uses": getattr(invite, "max_uses", None),
                "temporary": getattr(invite, "temporary", False),
                "inviter": getattr(inviter, "id", "Unknown"),
                "channel": getattr(invite_channel, "id", "Unknown"),
            }
            await self.save(guild)
        if not self.settings[guild.id]["invite_created"]["enabled"]:
            return
        try:
            channel = await self.modlog_channel(guild, "invite_created")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["invite_created"]["embed"]
        )
        invite_attrs = {
            "code": "Code:",
            "inviter": "Inviter:",
            "channel": "Channel:",
            "max_uses": "Max Uses:",
            "max_age": "Max Age:",
            "temporary": "Temporary:",
            "scheduled_event": "Scheduled Event:",
        }
        invite_time = invite.created_at or datetime.datetime.now(datetime.timezone.utc)
        msg = (
            f"{self.settings[guild.id]['invite_created']['emoji']} "
            f"{discord.utils.format_dt(invite_time)} Invite created "
        )
        embed = discord.Embed(
            title="Invite Created",
            colour=await self.get_event_colour(guild, "invite_created"),
            timestamp=invite_time,
        )
        worth_updating = False

        if getattr(invite, "inviter", None):
            embed.description = "{} created an invite for {}.".format(
                getattr(invite.inviter, "mention", invite.inviter),
                getattr(invite.channel, "mention", str(invite.channel)),
            )
        elif guild.widget_enabled and guild.widget_channel:
            embed.description = f"Widget in {guild.widget_channel.mention} created a new invite."
        if embed.description is None:
            embed.description = invite.url
        else:
            embed.description += f"\n{invite.url}"
        for attr, name in invite_attrs.items():
            before_attr = getattr(invite, attr, None)
            if before_attr:
                if attr == "max_age":
                    before_attr = humanize_timedelta(seconds=before_attr)
                if attr == "channel":
                    before_attr = getattr(before_attr, "mention", before_attr)
                if attr == "inviter":
                    before_attr = getattr(before_attr, "mention", before_attr)
                if attr == "code":
                    before_attr = box(before_attr)
                if attr == "scheduled_event":
                    before_attr = getattr(invite.scheduled_event, "url", "")
                worth_updating = True
                msg += f"{name} {before_attr}\n"
                embed.add_field(name=name, value=str(before_attr))
        if not worth_updating:
            return
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        guild = self.bot.get_guild(invite.guild.id)
        if guild is None:
            return
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if not self.settings[guild.id]["invite_deleted"]["enabled"]:
            return
        try:
            channel = await self.modlog_channel(guild, "invite_deleted")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["invite_deleted"]["embed"]
        )
        invite_attrs = {
            "code": "Code: ",
            "inviter": "Inviter: ",
            "channel": "Channel: ",
            "max_uses": "Max Uses: ",
            "uses": "Used: ",
            "max_age": "Max Age:",
            "temporary": "Temporary:",
            "scheduled_event": "Scheduled Event:",
        }
        invite_time = invite.created_at or datetime.datetime.now(datetime.timezone.utc)
        msg = (
            f"{self.settings[guild.id]['invite_deleted']['emoji']} "
            f"{discord.utils.format_dt(invite_time)} Invite deleted "
        )
        embed = discord.Embed(
            title="Invite Deleted",
            colour=await self.get_event_colour(guild, "invite_deleted"),
            timestamp=invite_time,
        )
        inviter = getattr(invite, "inviter", None)
        invite_channel = getattr(invite, "channel", None)
        if inviter is not None:
            embed.description = "{} deleted or used up an invite for {}.".format(
                inviter.mention,
                invite_channel.mention if invite_channel is not None else "Unknown channel",
            )
        elif guild.widget_enabled and guild.widget_channel:
            embed.description = f"Widget in {guild.widget_channel.mention} invite deleted or used up."
        if embed.description is None:
            embed.description = invite.url
        else:
            embed.description += f"\n{invite.url}"
        entry = await self.get_audit_log_entry(guild, invite, discord.AuditLogAction.invite_delete)
        perp = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)
        worth_updating = False
        for attr, name in invite_attrs.items():
            before_attr = getattr(invite, attr, None)
            if before_attr:
                if attr == "max_age":
                    before_attr = humanize_timedelta(seconds=before_attr)
                if attr == "channel":
                    before_attr = getattr(before_attr, "mention", before_attr)
                if attr == "inviter":
                    before_attr = getattr(before_attr, "mention", before_attr)
                if attr == "code":
                    before_attr = box(before_attr)
                if attr == "scheduled_event":
                    before_attr = getattr(invite.scheduled_event, "url", "")
                worth_updating = True
                msg += f"{name} {before_attr}\n"
                embed.add_field(name=name, value=str(before_attr))
        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
            perp_str = getattr(perp, "mention", str(perp))
            msg += "Deleted by" + f" {perp}.\n"
            embed.add_field(name="Deleted by", value=perp_str)
        if reason:
            msg += "Reason" + f": {reason}\n"
            embed.add_field(name="Reason", value=reason)
        if not worth_updating:
            return
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        guild = thread.guild
        if guild.id not in self.settings:
            return
        if not self.settings[guild.id]["thread_create"]["enabled"]:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if await self.is_ignored_channel(guild, thread.parent_id):
            return
        try:
            channel = await self.modlog_channel(guild, "thread_create")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["thread_create"]["embed"]
        )
        time = datetime.datetime.now(datetime.timezone.utc)
        channel_type = str(thread.type).replace("_", " ").title()
        embed = discord.Embed(
            description=thread.name,
            timestamp=time,
            colour=await self.get_event_colour(guild, "thread_create"),
        )
        embed.set_author(name=f"{channel_type} Thread Created ({thread.id})")
        entry = await self.get_audit_log_entry(guild, thread, discord.AuditLogAction.thread_create)
        perp = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)
        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
        if thread.owner:
            owner = thread.owner
        else:
            try:
                owner = await self.bot.fetch_user(thread.owner_id)
            except discord.HTTPException:
                owner = None
        embed.add_field(name="Thread", value=thread.mention)
        embed.add_field(name="Type", value=channel_type)
        perp_msg = ""
        if owner is not None:
            perp_msg = f"by {owner} (`{owner.id}`)"
            embed.add_field(name="Created by ", value=owner.mention)
        if reason:
            perp_msg += f" Reason: {reason}"
            embed.add_field(name="Reason ", value=reason, inline=False)
        embed.add_field(name="Thread ID", value=box(str(thread.id)))
        msg = (
            f"{self.settings[guild.id]['thread_create']['emoji']} {discord.utils.format_dt(time)} "
            f"{channel_type} channel created {perp_msg} {thread.mention}"
        )
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        if guild.id not in self.settings:
            return
        if not self.settings[guild.id]["thread_delete"]["enabled"]:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if await self.is_ignored_channel(guild, payload.parent_id):
            return
        try:
            channel = await self.modlog_channel(guild, "thread_delete")
        except RuntimeError:
            return
        # Bug fix (vs. ExtendedModLog): this used to read channel_delete's embed
        # flag instead of thread_delete's, so thread_delete's own embed toggle had
        # no effect. Read the correct key.
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["thread_delete"]["embed"]
        )
        channel_type = str(payload.thread_type).replace("_", " ").title()
        time = datetime.datetime.now(datetime.timezone.utc)
        parent = guild.get_channel(payload.parent_id)
        description = f"Thread in {parent.mention if parent else payload.parent_id}"
        if payload.thread:
            description = (
                f"{payload.thread.mention} thread in {parent.mention if parent else payload.parent_id}"
            )
        embed = discord.Embed(
            description=description,
            timestamp=time,
            colour=await self.get_event_colour(guild, "thread_delete"),
        )
        embed.set_author(name=f"{channel_type} Thread Deleted ({payload.thread_id})")
        entry = await self.get_audit_log_entry(
            guild, payload.thread_id, discord.AuditLogAction.thread_delete
        )
        perp = getattr(entry, "user", None)
        reason = getattr(entry, "reason", None)

        perp_msg = ""
        embed.add_field(name="Type", value=channel_type)
        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
            perp_msg = f"by {perp} (`{perp.id}`)"
            embed.add_field(name="Deleted by ", value=perp.mention)
        if reason:
            perp_msg += f" Reason: {reason}"
            embed.add_field(name="Reason ", value=reason, inline=False)
        embed.add_field(name="Thread ID", value=box(str(payload.thread_id)))
        msg = (
            f"{self.settings[guild.id]['thread_delete']['emoji']} {discord.utils.format_dt(time)} "
            f"{channel_type} channel deleted {perp_msg} #{description} ({payload.thread_id})"
        )
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        guild = before.guild
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if not self.settings[guild.id]["thread_change"]["enabled"]:
            return
        if await self.is_ignored_channel(guild, before):
            return
        try:
            channel = await self.modlog_channel(guild, "thread_change")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["thread_change"]["embed"]
        )
        channel_type = str(after.type).title()
        time = datetime.datetime.now(datetime.timezone.utc)
        embed = discord.Embed(
            description=after.mention,
            timestamp=time,
            colour=await self.get_event_colour(guild, "thread_change"),
        )
        embed.set_author(name=f"{channel_type} Thread Updated {before.name} ({before.id})")
        msg = (
            f"{self.settings[guild.id]['thread_change']['emoji']} {discord.utils.format_dt(time)} "
            f"Updated Thread {before.name}\n"
        )
        worth_updating = False
        perp = None
        reason = None
        text_updates = {
            "name": "Name",
            "slowmode_delay": "Slowmode delay",
            "auto_archive_duration": "Archive Duration",
            "locked": "Locked",
            "archived": "Archived",
        }
        before_changes = []
        after_changes = []
        for attr, name in text_updates.items():
            before_attr = getattr(before, attr)
            after_attr = getattr(after, attr)
            if before_attr != after_attr:
                worth_updating = True
                if before_attr == "":
                    before_attr = "None"
                if after_attr == "":
                    after_attr = "None"
                msg += f"Before {name}: {before_attr}\n" + f"After {name}: {after_attr}\n"
                before_changes.append(f"{name}: {before_attr}\n")
                after_changes.append(f"{name}: {after_attr}\n")
                entry = await self.get_audit_log_entry(
                    guild, before, discord.AuditLogAction.thread_update
                )
                perp = getattr(entry, "user", None)
                reason = getattr(entry, "reason", None)
        if before_changes or after_changes:
            embed.add_field(name="Before", value="".join(before_changes))
            embed.add_field(name="After", value="".join(after_changes))
        if before.archiver_id != after.archiver_id and after.archiver_id is not None:
            worth_updating = True
            member = before.guild.get_member(after.archiver_id)
            member_mention = member.mention if member is not None else "Unknown user"
            embed.add_field(name="Archived by:", value=member_mention)
            entry = await self.get_audit_log_entry(guild, before, discord.AuditLogAction.channel_update)
            perp = getattr(entry, "user", None)
            reason = getattr(entry, "reason", None)

        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
            msg += "Updated by " + str(perp) + "\n"
            embed.add_field(name="Updated by ", value=perp.mention)
        if reason:
            msg += "Reason " + reason + "\n"
            embed.add_field(name="Reason ", value=reason, inline=False)
        embed.add_field(name="Thread ID", value=box(str(after.id)))
        if not worth_updating:
            return
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)

    @commands.Cog.listener()
    async def on_guild_stickers_update(
        self,
        guild: discord.Guild,
        before: Sequence[discord.GuildSticker],
        after: Sequence[discord.GuildSticker],
    ) -> None:
        if guild.id not in self.settings:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        if guild.me.is_timed_out():
            return
        if not self.settings[guild.id]["stickers_change"]["enabled"]:
            return
        try:
            channel = await self.modlog_channel(guild, "stickers_change")
        except RuntimeError:
            return
        embed_links = (
            channel.permissions_for(guild.me).embed_links
            and self.settings[guild.id]["stickers_change"]["embed"]
        )
        time = datetime.datetime.now(datetime.timezone.utc)
        embed = discord.Embed(
            timestamp=time, colour=await self.get_event_colour(guild, "stickers_change")
        )
        embed.description = ""
        embed.set_author(name="Updated Server Stickers")
        msg = (
            f"{self.settings[guild.id]['stickers_change']['emoji']} {discord.utils.format_dt(time)} "
            "Updated Server Stickers"
        )
        worth_updating = False
        b = set(before)
        a = set(after)
        added_sticker: Optional[discord.GuildSticker] = None
        removed_sticker: Optional[discord.GuildSticker] = None
        try:
            added_sticker = (a - b).pop()
        except KeyError:
            pass
        try:
            removed_sticker = (b - a).pop()
        except KeyError:
            pass
        if added_sticker is not None:
            to_iter = before + (added_sticker,)
        else:
            to_iter = before
        changed_sticker = set((e, e.name) for e in after)
        changed_sticker.difference_update((e, e.name) for e in to_iter)
        old_sticker = None
        try:
            changed_sticker = changed_sticker.pop()[0]
        except KeyError:
            changed_sticker = None
        else:
            for old_sticker in before:
                if old_sticker.id == changed_sticker.id:
                    break
            else:
                changed_sticker = None
        action = None
        if removed_sticker is not None:
            worth_updating = True
            new_msg = f"`{removed_sticker}` (ID: {removed_sticker.id}) Removed from the guild\n"
            msg += new_msg
            embed.description += new_msg
            # discord.py has distinct sticker_create/sticker_delete/sticker_update audit
            # log actions (unlike ExtendedModLog, which reused the emoji_* actions here -
            # a copy/paste artifact from this listener being adapted from
            # on_guild_emojis_update). Use the correct sticker-specific actions.
            action = discord.AuditLogAction.sticker_delete
            embed.set_image(url=removed_sticker.url)
        elif added_sticker is not None:
            worth_updating = True
            new_msg = f"{added_sticker} `{added_sticker}` Added to the guild\n"
            msg += new_msg
            embed.description += new_msg
            action = discord.AuditLogAction.sticker_create
            embed.set_image(url=added_sticker.url)
        elif changed_sticker is not None:
            worth_updating = True
            sticker_name = f"{changed_sticker} `{changed_sticker}`"
            embed.set_image(url=changed_sticker.url)
            if old_sticker.name != changed_sticker.name:
                new_msg = (
                    f"{sticker_name} Renamed from {old_sticker.name} to {changed_sticker.name}\n"
                )
                action = discord.AuditLogAction.sticker_update
                msg += new_msg
                embed.description += new_msg
            if old_sticker.emoji != changed_sticker.emoji:
                new_msg = "{} emoji changed from {} to {}\n".format(
                    sticker_name, old_sticker.emoji, changed_sticker.emoji
                )
                action = discord.AuditLogAction.sticker_update
                msg += new_msg
                embed.description += new_msg
            if old_sticker.description != changed_sticker.description:
                new_msg = "{} description changed from {} to {}\n".format(
                    sticker_name, old_sticker.description, changed_sticker.description
                )
                action = discord.AuditLogAction.sticker_update
                msg += new_msg
                embed.description += new_msg

        perp = None
        reason = None
        if not worth_updating:
            return
        if channel.permissions_for(guild.me).view_audit_log:
            if action:
                entry = await self.get_audit_log_entry(guild, None, action)
                perp = getattr(entry, "user", None)
                reason = getattr(entry, "reason", None)
        if perp:
            if await self.is_ignored_mod(guild, perp):
                return
            embed.add_field(name="Updated by ", value=perp.mention)
            msg += "Updated by " + str(perp) + "\n"
        if reason:
            msg += "Reason " + reason + "\n"
            embed.add_field(name="Reason ", value=reason, inline=False)
        if embed_links:
            await self._safe_send(channel, embed=embed, allowed_mentions=self.allowed_mentions)
        else:
            await self._safe_send(channel, msg, allowed_mentions=self.allowed_mentions)
