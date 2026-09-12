"""AuditLog cog: extended server event logging on top of Red's core modlog.

From-scratch reimplementation of TrustyJAID's ExtendedModLog cog. It posts
embeds (or plain text) to configurable channels for roughly twenty kinds of
server events - message edits/deletes, member joins/leaves/updates, role and
channel and thread create/update/delete, guild updates, emoji/sticker
changes, voice state changes, bot command usage, and invite create/delete.

The per-guild Config schema is kept 1:1 with ExtendedModLog's own (see
settings.py) so `[p]auditlog migrate` can bring over an existing
ExtendedModLog setup with a straight per-guild dict copy - see
`AuditLog.auditlog_migrate` below.

Fixes applied relative to ExtendedModLog (see the corresponding comment at
each listener/command for details):

1. `role_change`'s custom colour setting now actually applies to role
   *updates*, not just role create/delete (eventmixin.py, on_guild_role_update).
2. Thread deletes read `thread_delete`'s own embed toggle instead of
   `channel_delete`'s (eventmixin.py, on_raw_thread_delete).
3. A member-flags-only change is no longer silently dropped
   (eventmixin.py, on_member_update).
4. `get_invite_link`'s "was this invite on its last use" fallback now
   actually runs, and no longer risks an unhandled TypeError on an
   unlimited-use invite (this file, get_invite_link).
5. No shared mutable defaults dict is ever aliased into a guild's live
   settings cache (this file, `_ensure_guild`) - every guild's cache entry
   is populated by reading Config, never by pointing at `inv_settings`
   itself, which is what let one guild's changes silently corrupt every
   other guild's settings in the original.
6. `[p]auditlog all`'s aliases (`all_settings`, `toggle_all`) actually work -
   the original's `aliaes=[...]` typo meant they never registered.
7. `[p]auditlog unignore channel` is properly named `channel` (symmetric
   with `[p]auditlog ignore channel`) - the original left it unnamed, so it
   was only reachable as `unignore unignore_channel`.
8. Every `channel.send(...)` is routed through `_safe_send`, which swallows
   `discord.HTTPException` (channel deleted mid-flight, an unexpected
   permission change, etc.) so one failed send can't crash a listener.

Gateway intents: `guilds`, `voice_states`, `invites`, and
`emojis_and_stickers` are all needed and are non-privileged (on by Red's
defaults, but still worth checking if you've trimmed intents down).
`message_content` (privileged) is needed for message_edit/message_delete to
show the actual message text - without it those events still fire, but the
before/after content is always empty. `members` (privileged) is needed for
member join/leave/update events to fire at all with useful data - discord.py
does not dispatch `on_member_update` (and `on_member_join`/`on_member_remove`
carry only minimal data) without it. `presences` is not used by this cog.
"""

import asyncio
import datetime
import logging
from typing import Any, Deque, Dict, List, Optional, Union

import discord
from discord.ext import tasks
from redbot.core import Config, commands, modlog
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import humanize_list, pagify

from .converters import CommandPrivs, EventChooser
from .eventmixin import AuditLogEventMixin, MemberUpdateEnum
from .settings import inv_settings

log = logging.getLogger("red.auditlog")

#: ExtendedModLog's own Config identifier/cog name - read-only, for migration.
OLD_CONFIG_IDENTIFIER = 154457677895
OLD_COG_NAME = "ExtendedModLog"

#: auditlog's own Config identifier. Distinct from ExtendedModLog's above.
AUDITLOG_CONFIG_IDENTIFIER = 615243879


def with_event_choices_help():
    """Append the common `[events...]` option list to a command's docstring.

    Must be the last decorator applied (i.e. written directly above `def`)
    for `functools`-style docstring stacking to work as expected.
    """
    added_doc = """

    `[events...]` may be any of (more than one may be given at once):
    - `channel_change` / `channel_create` / `channel_delete`
    - `commands_used` - bot command usage
    - `emoji_change` - emojis added, removed, or renamed
    - `stickers_change` - stickers added, removed, or renamed
    - `guild_change` - server settings changed
    - `message_edit` / `message_delete`
    - `member_change` - member changes: roles, nickname, timeout, etc.
    - `role_change` / `role_create` / `role_delete`
    - `voice_change` - voice channel join/leave/move
    - `member_join` / `member_left`
    - `invite_created` / `invite_deleted`
    - `thread_create` / `thread_delete` / `thread_change`
    """

    def decorator(func):
        func.__doc__ = (func.__doc__ or "") + added_doc
        return func

    return decorator


class AuditLog(AuditLogEventMixin, commands.Cog):
    """Log server events to configurable channels, extending Red's core modlog.

    This is a from-scratch reimplementation of TrustyJAID's ExtendedModLog
    cog. Use `[p]auditlog migrate` to bring over an existing ExtendedModLog
    setup for the current server.
    """

    __author__ = "Poag"
    __version__ = "1.0.0"

    def __init__(self, bot: Red) -> None:
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=AUDITLOG_CONFIG_IDENTIFIER, force_registration=True
        )
        self.config.register_guild(**inv_settings)
        self.config.register_global(schema_version=1)
        # In-memory cache mirroring each guild's Config entry. A guild with no
        # key here is never logged for anything - every listener's first real
        # check is `guild.id in self.settings`.
        self.settings: Dict[int, Any] = {}
        self._ban_cache: Dict[int, List[int]] = {}
        self.audit_log: Dict[int, Deque[discord.AuditLogEntry]] = {}
        self.allowed_mentions = discord.AllowedMentions(users=False, roles=False, everyone=False)

    def format_help_for_context(self, ctx: commands.Context) -> str:
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nCog Version: {self.__version__}"

    async def cog_load(self) -> None:
        for guild_id in await self.config.all_guilds():
            self.settings[int(guild_id)] = await self.config.guild_from_id(guild_id).all()
        self.invite_links_loop.start()

    async def cog_unload(self) -> None:
        self.invite_links_loop.cancel()

    async def red_delete_data_for_user(self, **kwargs) -> None:
        """Nothing to delete.

        Everything this cog stores is guild-scoped configuration that admins
        set themselves (routing channels, emoji, ignore lists, and a rolling
        invite-uses snapshot) - it never persists logged message content,
        join/leave history, or anything else *about* a user beyond what an
        admin chose to put in an ignore list. Ignore-list entries are admin
        configuration, not user data, and (matching ExtendedModLog) are left
        alone by data deletion requests; an admin can remove one with
        `[p]auditlog unignore`.
        """
        return

    #
    # Shared helpers
    #

    async def save(self, guild: discord.Guild) -> None:
        """Flush this guild's entire in-memory settings dict back to Config."""
        async with self.config.guild(guild).all() as all_settings:
            for key, value in self.settings[guild.id].items():
                all_settings[key] = value

    async def _ensure_guild(self, guild: discord.Guild) -> Dict[str, Any]:
        """Make sure `guild` has a live settings cache entry, and return it.

        Always populates a missing entry by reading Config - never by
        pointing at the shared `inv_settings` defaults dict directly, which
        would alias one guild's "live" settings to the same mutable object
        used as everyone else's defaults (see fix #5 in this module's
        docstring). Every command that can mutate settings should go through
        this instead of checking `ctx.guild.id in self.settings` by hand.
        """
        if guild.id not in self.settings:
            self.settings[guild.id] = await self.config.guild(guild).all()
        return self.settings[guild.id]

    async def _safe_send(self, channel: discord.abc.Messageable, *args, **kwargs) -> None:
        """`channel.send(...)`, but a failed send can't crash the caller's listener."""
        try:
            await channel.send(*args, **kwargs)
        except discord.HTTPException:
            log.warning("Failed to send an auditlog message to %s", channel, exc_info=True)

    async def get_event_colour(
        self,
        guild: discord.Guild,
        event_type: str,
        changed_object: Optional[discord.Role] = None,
    ) -> discord.Colour:
        if guild.text_channels:
            cmd_colour = await self.bot.get_embed_colour(guild.text_channels[0])
        else:
            cmd_colour = discord.Colour.red()
        defaults = {
            "message_edit": discord.Colour.orange(),
            "message_delete": discord.Colour.dark_red(),
            "user_change": discord.Colour.greyple(),
            "role_change": changed_object.colour if changed_object else discord.Colour.blue(),
            "role_create": discord.Colour.blue(),
            "role_delete": discord.Colour.dark_blue(),
            "voice_change": discord.Colour.magenta(),
            "user_join": discord.Colour.green(),
            "user_left": discord.Colour.dark_green(),
            "channel_change": discord.Colour.teal(),
            "channel_create": discord.Colour.teal(),
            "channel_delete": discord.Colour.dark_teal(),
            "guild_change": discord.Colour.blurple(),
            "emoji_change": discord.Colour.gold(),
            "stickers_change": discord.Colour.gold(),
            "commands_used": cmd_colour,
            "invite_created": discord.Colour.blurple(),
            "invite_deleted": discord.Colour.blurple(),
            "thread_change": discord.Colour.teal(),
            "thread_create": discord.Colour.teal(),
            "thread_delete": discord.Colour.dark_teal(),
        }
        colour = defaults[event_type]
        custom = self.settings[guild.id][event_type]["colour"]
        if custom is not None:
            colour = discord.Colour(custom)
        return colour

    async def is_ignored_channel(
        self,
        guild: discord.Guild,
        channel: Union[discord.abc.Messageable, discord.abc.GuildChannel, int],
    ) -> bool:
        ignored_channels = self.settings[guild.id]["ignored_channels"]
        if isinstance(channel, (int, discord.PartialMessageable)):
            # A thread's parent channel can be deleted, leaving `thread.parent`
            # None - `thread.parent_id` is always an int, so callers pass that
            # (or a bare channel id) through here directly in that case.
            if isinstance(channel, discord.PartialMessageable):
                channel = channel.id
            return channel in ignored_channels
        if channel.id in ignored_channels:
            return True
        if isinstance(channel, (discord.CategoryChannel, discord.DMChannel, discord.GroupChannel)):
            return True
        category = getattr(channel, "category", None)
        if category is not None and category.id in ignored_channels:
            return True
        if (
            isinstance(channel, discord.Thread)
            and channel.parent
            and channel.parent.id in ignored_channels
        ):
            return True
        return False

    async def is_ignored_user(
        self, guild: discord.Guild, user: Union[discord.User, discord.Member, int]
    ) -> bool:
        ignored_users = self.settings[guild.id]["ignored_users"]
        user_id = user if isinstance(user, int) else user.id
        return user_id in ignored_users

    async def is_ignored_mod(
        self, guild: discord.Guild, user: Union[discord.User, discord.Member, int]
    ) -> bool:
        ignored_mods = self.settings[guild.id]["ignored_mods"]
        user_id = user if isinstance(user, int) else user.id
        return user_id in ignored_mods

    async def modlog_channel(self, guild: discord.Guild, event: str) -> discord.TextChannel:
        channel = None
        settings = self.settings[guild.id].get(event)
        if settings and "channel" in settings and settings["channel"]:
            channel = guild.get_channel(settings["channel"])
        if channel is None:
            try:
                channel = await modlog.get_modlog_channel(guild)
            except RuntimeError:
                raise RuntimeError("No modlog channel set")
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("Configured modlog channel is not a text channel")
        if not channel.permissions_for(guild.me).send_messages:
            raise RuntimeError("No permission to send messages in the modlog channel")
        return channel

    async def get_audit_log_entry(
        self,
        guild: discord.Guild,
        target: Union[
            discord.abc.GuildChannel,
            discord.Thread,
            discord.Member,
            discord.User,
            discord.Role,
            discord.Invite,
            int,
            None,
        ],
        action: discord.AuditLogAction,
        *,
        extra: Optional[str] = None,
        removed_roles: Optional[List[discord.Role]] = None,
        added_roles: Optional[List[discord.Role]] = None,
        channel_id: Optional[int] = None,
    ) -> Optional[discord.AuditLogEntry]:
        """Best-effort match of an audit log entry to a given event.

        Ported faithfully from ExtendedModLog: checks the short-lived
        `on_audit_log_entry_create` cache first (after a fixed 5-second
        delay, since Discord's audit log can lag behind the event that
        caused it), then falls back to fetching the 5 most recent entries
        for that action directly. This is inherently a heuristic - the
        entry with a matching target/role-diff within the last 10 minutes
        is assumed to be the right one, which is why every caller treats a
        wrong/missing match as harmless (worst case: no perpetrator/reason
        shown, never a crash).
        """
        entry = None
        if isinstance(target, int) or target is None:
            target_id = target
        elif isinstance(target, discord.Invite):
            target_id = target.code
        else:
            target_id = target.id

        if not guild.me.guild_permissions.view_audit_log:
            return None

        # Wait for the audit log entry to catch up to the event that caused it
        # before looking anything up.
        await asyncio.sleep(5)
        max_age = datetime.timedelta(minutes=10)
        now = discord.utils.utcnow()

        # Prefer the push-based on_audit_log_entry_create cache - it's both
        # faster and doesn't cost an API call - and only fall back to an
        # explicit fetch of the most recent matching entries if nothing in the
        # cache matched. The cache is ordered oldest-to-newest (plain
        # `deque.append`), and this loop deliberately never breaks early -
        # each matching candidate overwrites `entry`, so the *last* (i.e.
        # most recent) qualifying candidate wins.
        for candidate in self.audit_log.get(guild.id, ()):
            if candidate.action != action:
                continue
            if (now - candidate.created_at) > max_age:
                continue
            entry, _ = self._match_audit_log_candidate(
                candidate,
                entry,
                target_id=target_id,
                channel_id=channel_id,
                action=action,
                extra=extra,
                removed_roles=removed_roles,
                added_roles=added_roles,
            )

        if entry is None:
            # `guild.audit_logs()` yields newest-first, so here (unlike the
            # cache scan above) we stop at the first plain target-id match -
            # it's already the most recent one available. Weaker (role-diff)
            # matches don't short-circuit the scan on their own, mirroring
            # ExtendedModLog: with up to 5 entries to look at, a plainer exact
            # match found on a later (older) entry is still preferred over a
            # fuzzy match found earlier.
            async for candidate in guild.audit_logs(limit=5, action=action):
                if (now - candidate.created_at) > max_age:
                    continue
                entry, exact_id_match = self._match_audit_log_candidate(
                    candidate,
                    entry,
                    target_id=target_id,
                    channel_id=channel_id,
                    action=action,
                    extra=extra,
                    removed_roles=removed_roles,
                    added_roles=added_roles,
                )
                if exact_id_match:
                    break
        return entry

    @staticmethod
    def _match_audit_log_candidate(
        log_entry: discord.AuditLogEntry,
        entry: Optional[discord.AuditLogEntry],
        *,
        target_id,
        channel_id: Optional[int],
        action: discord.AuditLogAction,
        extra: Optional[str],
        removed_roles: Optional[List[discord.Role]],
        added_roles: Optional[List[discord.Role]],
    ) -> "tuple[Optional[discord.AuditLogEntry], bool]":
        """Apply one candidate entry's worth of `get_audit_log_entry`'s matching rules.

        Returns `(entry, exact_id_match)` - `exact_id_match` is only True when
        this candidate matched by plain target id/invite code while nothing
        had matched yet; see the fetch-loop comment in `get_audit_log_entry`
        for why only that case is used to stop scanning early.
        """
        if channel_id is not None and action is discord.AuditLogAction.message_delete:
            ex = getattr(log_entry, "extra", None)
            if ex is not None and ex.channel is not None and ex.channel.id == channel_id:
                entry = log_entry

        if extra is not None and getattr(log_entry.after, extra, None) is None:
            return entry, False

        if log_entry.action is discord.AuditLogAction.member_role_update:
            log_before = getattr(log_entry.before, "roles", [])
            log_after = getattr(log_entry.after, "roles", [])
            log_removed = [r.id for r in set(log_before) - set(log_after)]
            log_added = [r.id for r in set(log_after) - set(log_before)]
            if added_roles:
                a_roles = [r.id for r in added_roles]
                if set(a_roles) == set(log_added):
                    entry = log_entry
                else:
                    for role in added_roles:
                        if role.id in log_added:
                            entry = log_entry
            if removed_roles:
                r_roles = [r.id for r in removed_roles]
                if set(r_roles) == set(log_removed):
                    entry = log_entry
                else:
                    for role in removed_roles:
                        if role.id in log_added:
                            entry = log_entry

        exact_id_match = False
        if target_id == getattr(log_entry.target, "id", None) and entry is None:
            entry = log_entry
            exact_id_match = True
        if target_id == getattr(log_entry.target, "code", None):
            entry = log_entry
        return entry, exact_id_match

    async def get_permission_change(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> str:
        p_msg = ""
        before_perms = {}
        after_perms = {}
        guild = before.guild
        for o, p in before.overwrites.items():
            before_perms[str(o.id)] = [i for i in p]
        for o, p in after.overwrites.items():
            after_perms[str(o.id)] = [i for i in p]
        for entity in before_perms:
            entity_obj = before.guild.get_role(int(entity)) or before.guild.get_member(int(entity))
            name = entity_obj.mention if entity_obj is not None else entity
            if entity not in after_perms:
                entry = await self.get_audit_log_entry(
                    guild, before, discord.AuditLogAction.overwrite_delete
                )
                perp = getattr(entry, "user", None)
                if perp:
                    p_msg += f"{perp.mention} Removed overwrites:\n"
                p_msg += f"{name} Overwrites removed:\n"
                for diff in set(before_perms[entity]):
                    if diff[1] is None:
                        continue
                    p_msg += f"{name} {diff[0]} Reset.\n"
                continue
            if after_perms[entity] != before_perms[entity]:
                entry = await self.get_audit_log_entry(
                    guild, before, discord.AuditLogAction.overwrite_update
                )
                perp = getattr(entry, "user", None)
                if perp:
                    p_msg += f"{perp.mention} Updated overwrites:\n"
                a_perms = list(set(after_perms[entity]) - set(before_perms[entity]))
                for diff in a_perms:
                    p_msg += "- {} {} Set to {}.\n".format(
                        name, diff[0].replace("_", " ").title(), diff[1]
                    )
        for entity in after_perms:
            entity_obj = after.guild.get_role(int(entity)) or after.guild.get_member(int(entity))
            name = entity_obj.mention if entity_obj is not None else entity
            if entity not in before_perms:
                entry = await self.get_audit_log_entry(
                    guild, before, discord.AuditLogAction.overwrite_update
                )
                perp = getattr(entry, "user", None)
                if perp:
                    p_msg += f"{perp.mention} Added overwrites:\n"
                p_msg += f"{name} Overwrites added.\n"
                for diff in set(after_perms[entity]):
                    if diff[1] is None:
                        continue
                    p_msg += "- {} {} Set to {}.\n".format(
                        name, diff[0].replace("_", " ").title(), diff[1]
                    )
                continue
        return p_msg

    async def get_role_permission_change(self, before: discord.Role, after: discord.Role) -> str:
        p_msg = ""
        changed_perms = dict(after.permissions).items() - dict(before.permissions).items()
        for p, change in changed_perms:
            p_msg += f"- {p.replace('_', ' ').title()} Set to **{change}**\n"
        return p_msg

    #
    # Invite link tracking
    #

    @tasks.loop(seconds=300)
    async def invite_links_loop(self) -> None:
        """Refresh the invite-uses snapshot every 5 minutes for guilds logging joins."""
        for guild_id in list(self.settings.keys()):
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            if self.settings[guild_id]["user_join"]["enabled"]:
                await self.save_invite_links(guild)

    @invite_links_loop.before_loop
    async def before_invite_links_loop(self) -> None:
        await self.bot.wait_until_red_ready()

    async def save_invite_links(self, guild: discord.Guild) -> bool:
        invites = {}
        if not guild.me.guild_permissions.manage_guild:
            return False
        try:
            for invite in await guild.invites():
                created_at = getattr(invite, "created_at", discord.utils.utcnow())
                channel = getattr(invite, "channel", discord.Object(id=0))
                inviter = getattr(invite, "inviter", discord.Object(id=0))
                invites[invite.code] = {
                    "uses": getattr(invite, "uses", 0),
                    "max_age": getattr(invite, "max_age", None),
                    "created_at": created_at.timestamp(),
                    "max_uses": getattr(invite, "max_uses", None),
                    "temporary": getattr(invite, "temporary", False),
                    "inviter": getattr(inviter, "id", "Unknown"),
                    "channel": getattr(channel, "id", "Unknown"),
                }
        except discord.HTTPException:
            log.warning("Error saving invites for guild %s (Discord server error).", guild.id)
            return False
        except Exception:
            log.exception("Error saving invites for guild %s.", guild.id)
            return False

        self.settings[guild.id]["invite_links"] = invites
        await self.save(guild)
        return True

    async def get_invite_link(self, member: discord.Member) -> str:
        guild = member.guild
        manage_guild = guild.me.guild_permissions.manage_guild
        invites = self.settings[guild.id]["invite_links"]
        possible_link = ""
        check_logs = manage_guild and guild.me.guild_permissions.view_audit_log
        if member.bot:
            if check_logs:
                entry = await self.get_audit_log_entry(
                    guild, member, discord.AuditLogAction.bot_add
                )
                if entry:
                    possible_link = f"Added by: {entry.user}"
            return possible_link
        if manage_guild and "VANITY_URL" in guild.features:
            try:
                possible_link = str(await guild.vanity_invite())
            except (discord.NotFound, discord.HTTPException):
                pass

        if invites and manage_guild:
            guild_invites = await guild.invites()
            for invite in guild_invites:
                if invite.code not in invites:
                    continue
                uses = invites[invite.code]["uses"]
                if invite.uses is None or uses is None:
                    continue
                if invite.uses > uses:
                    possible_link = "https://discord.gg/{}\nInvited by: {}".format(
                        invite.code, getattr(invite.inviter, "mention", "Widget Integration")
                    )
                    break

            if not possible_link:
                # Bug fix (vs. ExtendedModLog): the original's fallback here was
                # guarded by a `try/except Exception: ... pass` whose body then did
                # unguarded arithmetic (`max_uses - uses`) that raises TypeError
                # whenever an invite has no max_uses (i.e. unlimited uses) - a very
                # common case - silently aborting the whole join-attribution lookup
                # (and the invite_links re-save after it) instead of falling through
                # cleanly. This version guards that arithmetic and always finishes
                # the loop and re-save regardless of any one invite's shape.
                for code, data in invites.items():
                    try:
                        await self.bot.fetch_invite(code)
                    except discord.NotFound:
                        # The invite is gone entirely. If it was one use away from
                        # its limit, it's a good bet this join used it up.
                        max_uses = data.get("max_uses")
                        used = data.get("uses")
                        if max_uses and used is not None and (max_uses - used) == 1:
                            inviter_id = data.get("inviter")
                            inviter = guild.get_member(inviter_id)
                            if inviter is None:
                                try:
                                    inviter = await self.bot.fetch_user(inviter_id)
                                except (discord.NotFound, discord.HTTPException):
                                    inviter = None
                            inviter_display = (
                                inviter.mention
                                if inviter is not None
                                else f"Unknown or deleted user ({inviter_id})"
                            )
                            possible_link = f"https://discord.gg/{code}\nInvited by: {inviter_display}"
                            break
                    except discord.HTTPException:
                        log.warning("Error fetching invite %s for guild %s", code, guild.id)
            await self.save_invite_links(guild)
        if check_logs and not possible_link:
            entry = await self.get_audit_log_entry(guild, None, discord.AuditLogAction.invite_create)
            if entry and entry.target is not None:
                invite = entry.target
                possible_link = "https://discord.gg/{}\nInvited by: {}".format(
                    invite.code, getattr(invite.inviter, "mention", "Unknown")
                )
        return possible_link

    #
    # Settings display
    #

    async def _send_settings(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        try:
            modlog_chan = (await modlog.get_modlog_channel(guild)).mention
        except Exception:
            modlog_chan = "Not Set"
        cur_settings = {
            "message_edit": "Message edits",
            "message_delete": "Message delete",
            "user_change": "Member changes",
            "role_change": "Role changes",
            "role_create": "Role created",
            "role_delete": "Role deleted",
            "voice_change": "Voice changes",
            "user_join": "Member join",
            "user_left": "Member left",
            "channel_change": "Channel changes",
            "channel_create": "Channel created",
            "channel_delete": "Channel deleted",
            "thread_create": "Thread created",
            "thread_delete": "Thread deleted",
            "thread_change": "Thread changed",
            "guild_change": "Guild changes",
            "emoji_change": "Emoji changes",
            "stickers_change": "Stickers changes",
            "commands_used": "Commands",
            "invite_created": "Invite created",
            "invite_deleted": "Invite deleted",
        }
        data = await self._ensure_guild(guild)
        msg = f"Settings for {guild.name}\nModlog Channel: {modlog_chan}\n\n"

        # Prune stale entries (deleted channels) as we go, same as ExtendedModLog.
        ignored_channels = []
        for c in list(data["ignored_channels"]):
            chn = guild.get_channel(c)
            if chn is None:
                data["ignored_channels"].remove(c)
            else:
                ignored_channels.append(chn)
        ignored_users = [f"<@{uid}>" for uid in data["ignored_users"]]
        ignored_mods = [f"<@{uid}>" for uid in data["ignored_mods"]]

        for key, name in cur_settings.items():
            msg += f"{name}: **{data[key]['enabled']}**"
            if key == "commands_used":
                msg += "\n" + humanize_list(data[key]["privs"])
            if data[key]["channel"]:
                chn = guild.get_channel(data[key]["channel"])
                if chn is None:
                    data[key]["channel"] = None
                    msg += "\n"
                else:
                    msg += f" {chn.mention}\n"
            else:
                msg += "\n"

        if ignored_channels:
            msg += "Ignored Channels: " + ", ".join(c.mention for c in ignored_channels) + "\n"
        if ignored_users:
            msg += "Ignored Users: " + humanize_list(ignored_users) + "\n"
        if ignored_mods:
            msg += "Ignored Mods: " + humanize_list(ignored_mods) + "\n"

        # Save back in case we pruned any stale channel ids above.
        await self.save(guild)

        if await ctx.embed_requested():
            for page in pagify(msg, page_length=4000):
                await ctx.send(embed=discord.Embed(description=page))
        else:
            for page in pagify(msg):
                await ctx.send(page, allowed_mentions=self.allowed_mentions)

    async def _members_settings(self, ctx: commands.Context, msg: str = "") -> None:
        guild = ctx.guild
        if guild is None:
            return
        msg += f"\n### Member logging Settings for {guild.name}\n"
        data = (await self._ensure_guild(guild))["user_change"]
        for update_type in MemberUpdateEnum:
            msg += f"{update_type.get_name()}: **{data[update_type.name]}**\n"
        await ctx.maybe_send_embed(msg)

    #
    # Command group
    #

    @commands.admin_or_permissions(manage_channels=True)
    @commands.group(name="auditlog", aliases=["auditlogtoggle", "auditlogs"])
    @commands.guild_only()
    async def auditlog(self, ctx: commands.Context) -> None:
        """Toggle extended server event logging.

        Requires a channel to be set up with `[p]modlogset modlog #channel`,
        or events can be routed to their own channels individually with
        `[p]auditlog channel #channel <event...>`.
        """

    @auditlog.command(name="settings")
    async def auditlog_settings_cmd(self, ctx: commands.Context) -> None:
        """Show this server's current auditlog settings."""
        await self._send_settings(ctx)

    @auditlog.command(name="colour", aliases=["color"])
    @with_event_choices_help()
    async def auditlog_colour(
        self, ctx: commands.Context, colour: discord.Colour, *events: EventChooser
    ) -> None:
        """Set a custom colour for one or more events' embeds.

        `<colour>` may be a hex code or a named discord.py colour.
        """
        if not events:
            await ctx.send("You must provide which events should be included.")
            return
        data = await self._ensure_guild(ctx.guild)
        for event in events:
            data[event]["colour"] = colour.value
        await self.save(ctx.guild)
        await ctx.send(
            "{} colour has been set to {}".format(
                humanize_list([e.replace("user_", "member_") for e in events]), colour
            )
        )

    @auditlog.command(name="embeds", aliases=["embed"])
    @with_event_choices_help()
    async def auditlog_embeds(
        self, ctx: commands.Context, true_or_false: bool, *events: EventChooser
    ) -> None:
        """Toggle embeds (as opposed to plain text) for one or more events."""
        if not events:
            await ctx.send("You must provide which events should be included.")
            return
        data = await self._ensure_guild(ctx.guild)
        for event in events:
            data[event]["embed"] = true_or_false
        await self.save(ctx.guild)
        await ctx.send(
            "{} embed logs have been set to {}".format(
                humanize_list([e.replace("user_", "member_") for e in events]), true_or_false
            )
        )

    @auditlog.command(name="emojiset")
    @commands.bot_has_permissions(add_reactions=True)
    @with_event_choices_help()
    async def auditlog_emojiset(
        self, ctx: commands.Context, emoji: Union[discord.Emoji, str], *events: EventChooser
    ) -> None:
        """Set the emoji used in plain-text (non-embed) logs for one or more events."""
        if not events:
            await ctx.send("You must provide which events should be included.")
            return
        data = await self._ensure_guild(ctx.guild)
        if isinstance(emoji, str):
            try:
                await ctx.message.add_reaction(emoji)
            except discord.HTTPException:
                await ctx.send(f"{emoji} is not a valid emoji.")
                return
        new_emoji = str(emoji)
        for event in events:
            data[event]["emoji"] = new_emoji
        await self.save(ctx.guild)
        await ctx.send(
            "{} emoji has been set to {}".format(
                humanize_list([e.replace("user_", "member_") for e in events]), new_emoji
            )
        )

    @auditlog.command(name="toggle")
    @with_event_choices_help()
    async def auditlog_toggle(
        self, ctx: commands.Context, true_or_false: bool, *events: EventChooser
    ) -> None:
        """Turn one or more events' logging on or off."""
        if not events:
            await ctx.send("You must provide which events should be included.")
            return
        data = await self._ensure_guild(ctx.guild)
        for event in events:
            data[event]["enabled"] = true_or_false
        await self.save(ctx.guild)
        await ctx.send(
            "{} logs have been set to {}".format(
                humanize_list([e.replace("user_", "member_") for e in events]), true_or_false
            )
        )

    @auditlog.command(name="channel")
    @with_event_choices_help()
    async def auditlog_channel(
        self, ctx: commands.Context, channel: discord.TextChannel, *events: EventChooser
    ) -> None:
        """Send one or more events to a specific channel, overriding the default modlog channel."""
        if not events:
            await ctx.send("You must provide which events should be included.")
            return
        data = await self._ensure_guild(ctx.guild)
        for event in events:
            data[event]["channel"] = channel.id
        await self.save(ctx.guild)
        await ctx.send(
            "{} logs have been set to {}".format(
                humanize_list([e.replace("user_", "member_") for e in events]), channel.mention
            )
        )

    @auditlog.command(name="resetchannel")
    @with_event_choices_help()
    async def auditlog_resetchannel(self, ctx: commands.Context, *events: EventChooser) -> None:
        """Reset one or more events back to the default modlog channel."""
        if not events:
            await ctx.send("You must provide which events should be included.")
            return
        data = await self._ensure_guild(ctx.guild)
        for event in events:
            data[event]["channel"] = None
        await self.save(ctx.guild)
        await ctx.send(f"{humanize_list(events)} logs' channel override has been reset.")

    @auditlog.command(name="all", aliases=["all_settings", "toggle_all"])
    async def auditlog_all(self, ctx: commands.Context, true_or_false: bool) -> None:
        """Turn every loggable event on or off at once."""
        data = await self._ensure_guild(ctx.guild)
        for setting in data.values():
            if isinstance(setting, dict) and "enabled" in setting:
                setting["enabled"] = true_or_false
        await self.save(ctx.guild)
        await self._send_settings(ctx)

    @auditlog.group(name="delete")
    async def auditlog_delete(self, ctx: commands.Context) -> None:
        """Message-delete-specific logging settings."""

    @auditlog_delete.command(name="bulkdelete")
    async def auditlog_delete_bulkdelete(self, ctx: commands.Context) -> None:
        """Toggle bulk message delete notifications."""
        data = await self._ensure_guild(ctx.guild)
        data["message_delete"]["bulk_enabled"] = not data["message_delete"]["bulk_enabled"]
        await self.save(ctx.guild)
        verb = "enabled" if data["message_delete"]["bulk_enabled"] else "disabled"
        await ctx.send(f"Bulk message delete logs {verb}.")

    @auditlog_delete.command(name="individual")
    async def auditlog_delete_individual(self, ctx: commands.Context) -> None:
        """Toggle replaying each cached message from a bulk delete individually."""
        data = await self._ensure_guild(ctx.guild)
        data["message_delete"]["bulk_individual"] = not data["message_delete"]["bulk_individual"]
        await self.save(ctx.guild)
        verb = "enabled" if data["message_delete"]["bulk_individual"] else "disabled"
        await ctx.send(f"Individual message delete logs for bulk message delete {verb}.")

    @auditlog_delete.command(name="cachedonly")
    async def auditlog_delete_cachedonly(self, ctx: commands.Context) -> None:
        """Toggle delete notifications for messages that weren't in the bot's cache.

        Notifications for non-cached messages only ever show channel info,
        never the deleted content or its author (Discord doesn't tell us
        either for messages it didn't have cached).
        """
        data = await self._ensure_guild(ctx.guild)
        data["message_delete"]["cached_only"] = not data["message_delete"]["cached_only"]
        await self.save(ctx.guild)
        verb = "disabled" if data["message_delete"]["cached_only"] else "enabled"
        await ctx.send(f"Delete logs for non-cached messages {verb}.")

    @auditlog_delete.command(name="ignorecommands")
    async def auditlog_delete_ignorecommands(self, ctx: commands.Context) -> None:
        """Toggle delete notifications for messages that were valid bot commands."""
        data = await self._ensure_guild(ctx.guild)
        data["message_delete"]["ignore_commands"] = not data["message_delete"]["ignore_commands"]
        await self.save(ctx.guild)
        verb = "enabled" if data["message_delete"]["ignore_commands"] else "disabled"
        await ctx.send(f"Ignoring deleted command messages {verb}.")

    @auditlog.group(name="member", aliases=["members", "memberchanges"])
    async def auditlog_member(self, ctx: commands.Context) -> None:
        """Toggle individual member-update settings."""

    @auditlog_member.command(name="settings")
    async def auditlog_member_settings(self, ctx: commands.Context) -> None:
        """Show current member-update logging settings."""
        await self._members_settings(ctx)

    @auditlog_member.command(name="nickname", aliases=["nicknames"])
    async def auditlog_member_nickname(self, ctx: commands.Context) -> None:
        """Toggle nickname changes in member-change logs."""
        data = await self._ensure_guild(ctx.guild)
        setting = data["user_change"]["nicknames"]
        data["user_change"]["nicknames"] = not setting
        await self.save(ctx.guild)
        verb = "no longer be" if setting else "be"
        await self._members_settings(ctx, f"Nicknames will {verb} tracked in member change logs.")

    @auditlog_member.command(name="avatar")
    async def auditlog_member_avatar(self, ctx: commands.Context) -> None:
        """Toggle guild-avatar changes in member-change logs."""
        data = await self._ensure_guild(ctx.guild)
        setting = data["user_change"]["avatar"]
        data["user_change"]["avatar"] = not setting
        await self.save(ctx.guild)
        verb = "no longer be" if setting else "be"
        await self._members_settings(ctx, f"Avatars will {verb} tracked in member change logs.")

    @auditlog_member.command(name="roles", aliases=["role"])
    async def auditlog_member_roles(self, ctx: commands.Context) -> None:
        """Toggle role add/remove changes in member-change logs."""
        data = await self._ensure_guild(ctx.guild)
        setting = data["user_change"]["roles"]
        data["user_change"]["roles"] = not setting
        await self.save(ctx.guild)
        verb = "no longer be" if setting else "be"
        await self._members_settings(ctx, f"Roles will {verb} tracked in member change logs.")

    @auditlog_member.command(name="pending")
    async def auditlog_member_pending(self, ctx: commands.Context) -> None:
        """Toggle membership-screening ("pending") changes in member-change logs."""
        data = await self._ensure_guild(ctx.guild)
        setting = data["user_change"]["pending"]
        data["user_change"]["pending"] = not setting
        await self.save(ctx.guild)
        verb = "no longer be" if setting else "be"
        await self._members_settings(ctx, f"Pending will {verb} tracked in member change logs.")

    @auditlog_member.command(name="timeout")
    async def auditlog_member_timeout(self, ctx: commands.Context) -> None:
        """Toggle timeout changes in member-change logs.

        Note: due to a Discord limitation this won't update when a member's
        timeout naturally expires, and may keep showing a past timeout.
        """
        data = await self._ensure_guild(ctx.guild)
        setting = data["user_change"]["timeout"]
        data["user_change"]["timeout"] = not setting
        await self.save(ctx.guild)
        verb = "no longer be" if setting else "be"
        await self._members_settings(ctx, f"Timeout will {verb} tracked in member change logs.")

    @auditlog_member.command(name="flags")
    async def auditlog_member_flags(self, ctx: commands.Context) -> None:
        """Toggle member flag changes (did_rejoin, completed_onboarding, etc.) in member-change logs."""
        data = await self._ensure_guild(ctx.guild)
        setting = data["user_change"]["flags"]
        data["user_change"]["flags"] = not setting
        await self.save(ctx.guild)
        verb = "no longer be" if setting else "be"
        await self._members_settings(
            ctx, f"Member flags will {verb} tracked in member change logs."
        )

    @auditlog_member.command(name="all")
    async def auditlog_member_all(self, ctx: commands.Context, set_to: bool) -> None:
        """Set every member-update sub-setting at once."""
        data = await self._ensure_guild(ctx.guild)
        for update_type in MemberUpdateEnum:
            data["user_change"][update_type.name] = set_to
        await self.save(ctx.guild)
        await self._members_settings(ctx)

    @auditlog.command(name="commandlevel", aliases=["commandslevel"])
    async def auditlog_commandlevel(self, ctx: commands.Context, *level: CommandPrivs) -> None:
        """Set which command privilege levels get logged.

        `[level...]` any combination of `NONE`, `MOD`, `ADMIN`, `GUILD_OWNER`,
        `BOT_OWNER`. `NONE` is a command anyone can use; `MOD` includes
        anything gated by `mod_or_permissions`, etc.
        """
        if not level:
            await ctx.send_help()
            return
        data = await self._ensure_guild(ctx.guild)
        data["commands_used"]["privs"] = list(level)
        await self.save(ctx.guild)
        await ctx.send("Command logs set to: " + humanize_list(level))

    @auditlog.group()
    async def ignore(self, ctx: commands.Context) -> None:
        """Ignore a channel, user, or mod from auditlog events."""

    @ignore.command(name="channel")
    async def ignore_channel(
        self,
        ctx: commands.Context,
        channel: Union[
            discord.TextChannel, discord.ForumChannel, discord.CategoryChannel, discord.VoiceChannel
        ],
    ) -> None:
        """Ignore a channel (or category) from message delete/edit events and bot commands."""
        data = await self._ensure_guild(ctx.guild)
        if channel.id in data["ignored_channels"]:
            await ctx.send(f"{channel.mention} is already being ignored.")
            return
        data["ignored_channels"].append(channel.id)
        await self.save(ctx.guild)
        await ctx.send(f"Now ignoring events in {channel.mention}.")

    @ignore.command(name="user")
    async def ignore_user(
        self, ctx: commands.Context, user: Union[discord.User, discord.Member]
    ) -> None:
        """Ignore a user from any event where they're the subject (message_x, member_change, ...)."""
        data = await self._ensure_guild(ctx.guild)
        if user.id in data["ignored_users"]:
            await ctx.send(f"{user.mention} is already ignored.", allowed_mentions=self.allowed_mentions)
            return
        data["ignored_users"].append(user.id)
        await self.save(ctx.guild)
        await ctx.send(f"Now ignoring {user.mention}.", allowed_mentions=self.allowed_mentions)

    @ignore.command(name="mod")
    async def ignore_mod(
        self, ctx: commands.Context, user: Union[discord.User, discord.Member]
    ) -> None:
        """Ignore a mod so events they perform (per the audit log) aren't logged.

        This may make events unexpectedly stop showing entirely, since
        looking up the audit log doesn't guarantee the perpetrator found is
        actually who performed the action.
        """
        data = await self._ensure_guild(ctx.guild)
        if user.id in data["ignored_mods"]:
            await ctx.send(
                f"I am already ignoring mod actions from {user.mention}.",
                allowed_mentions=self.allowed_mentions,
            )
            return
        data["ignored_mods"].append(user.id)
        await self.save(ctx.guild)
        await ctx.send(
            f"Now ignoring mod actions from {user.mention}.", allowed_mentions=self.allowed_mentions
        )

    @auditlog.group()
    async def unignore(self, ctx: commands.Context) -> None:
        """Stop ignoring a channel, user, or mod."""

    @unignore.command(name="channel")
    async def unignore_channel(
        self,
        ctx: commands.Context,
        channel: Union[
            discord.TextChannel, discord.ForumChannel, discord.CategoryChannel, discord.VoiceChannel
        ],
    ) -> None:
        """Stop ignoring a channel (or category)."""
        data = await self._ensure_guild(ctx.guild)
        if channel.id not in data["ignored_channels"]:
            await ctx.send(f"{channel.mention} is not being ignored.")
            return
        data["ignored_channels"].remove(channel.id)
        await self.save(ctx.guild)
        await ctx.send(f"Now tracking events in {channel.mention}.")

    @unignore.command(name="user")
    async def unignore_user(
        self, ctx: commands.Context, user: Union[discord.User, discord.Member]
    ) -> None:
        """Stop ignoring a user."""
        data = await self._ensure_guild(ctx.guild)
        if user.id not in data["ignored_users"]:
            await ctx.send(f"{user.mention} is not being ignored.", allowed_mentions=self.allowed_mentions)
            return
        data["ignored_users"].remove(user.id)
        await self.save(ctx.guild)
        await ctx.send(
            f"Now logging events from {user.mention}.", allowed_mentions=self.allowed_mentions
        )

    @unignore.command(name="mod")
    async def unignore_mod(
        self, ctx: commands.Context, user: Union[discord.User, discord.Member]
    ) -> None:
        """Stop ignoring a mod's actions."""
        data = await self._ensure_guild(ctx.guild)
        if user.id not in data["ignored_mods"]:
            await ctx.send(
                f"{user.mention} is not being ignored for mod actions.",
                allowed_mentions=self.allowed_mentions,
            )
            return
        data["ignored_mods"].remove(user.id)
        await self.save(ctx.guild)
        await ctx.send(
            f"Now logging mod actions from {user.mention}.", allowed_mentions=self.allowed_mentions
        )

    @auditlog.group(name="bot", aliases=["bots"])
    async def auditlog_bot(self, ctx: commands.Context) -> None:
        """Bot-account filter settings."""

    @auditlog_bot.command(name="edits", aliases=["edit"])
    async def auditlog_bot_edits(self, ctx: commands.Context) -> None:
        """Toggle message edit notifications for bot accounts."""
        data = await self._ensure_guild(ctx.guild)
        data["message_edit"]["bots"] = not data["message_edit"]["bots"]
        await self.save(ctx.guild)
        verb = "enabled" if data["message_edit"]["bots"] else "disabled"
        await ctx.send(f"Bot edited messages {verb}.")

    @auditlog_bot.command(name="deletes", aliases=["delete"])
    async def auditlog_bot_deletes(self, ctx: commands.Context) -> None:
        """Toggle message delete notifications for bot accounts.

        Only affects messages that are in the bot's own cache.
        """
        data = await self._ensure_guild(ctx.guild)
        data["message_delete"]["bots"] = not data["message_delete"]["bots"]
        await self.save(ctx.guild)
        verb = "enabled" if data["message_delete"]["bots"] else "disabled"
        await ctx.send(f"Bot delete logs {verb}.")

    @auditlog_bot.command(name="change")
    async def auditlog_bot_change(self, ctx: commands.Context) -> None:
        """Toggle bot accounts being logged in member-update events (roles, nickname, etc.)."""
        data = await self._ensure_guild(ctx.guild)
        setting = data["user_change"]["bots"]
        data["user_change"]["bots"] = not setting
        await self.save(ctx.guild)
        verb = "no longer be" if setting else "be"
        await ctx.send(f"Bots will {verb} tracked in member change logs.")

    @auditlog_bot.command(name="voice")
    async def auditlog_bot_voice(self, ctx: commands.Context) -> None:
        """Toggle bot accounts being logged in voice state update events."""
        data = await self._ensure_guild(ctx.guild)
        setting = data["voice_change"]["bots"]
        data["voice_change"]["bots"] = not setting
        await self.save(ctx.guild)
        verb = "no longer be" if setting else "be"
        await ctx.send(f"Bots will {verb} tracked in voice update logs.")

    #
    # Migration
    #

    @auditlog.command(name="migrate")
    async def auditlog_migrate(self, ctx: commands.Context) -> None:
        """Migrate this server's ExtendedModLog data into auditlog.

        Reads ExtendedModLog's own stored configuration directly (it does
        not need to be loaded). Since the schema is kept 1:1 with
        ExtendedModLog, this is a straight per-guild dict copy: every event
        toggle, channel override, colour/emoji/embed setting, the ignore
        lists, and the invite-uses snapshot. Safe to run more than once.
        """
        guild = ctx.guild
        old_config = Config.get_conf(
            cog_instance=None,
            identifier=OLD_CONFIG_IDENTIFIER,
            force_registration=False,
            cog_name=OLD_COG_NAME,
        )
        old_config.register_guild(**inv_settings)

        old_data = await old_config.guild(guild).all()
        # Deep-copy-by-reconstruction: write it into our own Config wholesale,
        # then re-read our own Config back into the in-memory cache rather than
        # keeping a reference to `old_data` itself, so nothing ends up aliased
        # to the other cog's config accessor internals.
        await self.config.guild(guild).set(old_data)
        self.settings[guild.id] = await self.config.guild(guild).all()

        enabled_events = [
            key.replace("user_", "member_")
            for key, value in self.settings[guild.id].items()
            if isinstance(value, dict) and value.get("enabled")
        ]
        ignored_channels = len(self.settings[guild.id]["ignored_channels"])
        ignored_users = len(self.settings[guild.id]["ignored_users"])
        ignored_mods = len(self.settings[guild.id]["ignored_mods"])
        invite_count = len(self.settings[guild.id]["invite_links"])

        msg = (
            "Migrated ExtendedModLog's settings for **{guild}** into auditlog.\n\n"
            "**Enabled events:** {events}\n"
            "**Ignored channels:** {ic}\n"
            "**Ignored users:** {iu}\n"
            "**Ignored mods:** {im}\n"
            "**Invite snapshot carried over:** {invites} invite(s)\n\n"
            "Once you've confirmed auditlog is working correctly, run "
            "`[p]unload extendedmodlog` to fully retire the old cog. "
            "This command does not do that for you."
        ).format(
            guild=guild.name,
            events=humanize_list(enabled_events) if enabled_events else "None",
            ic=ignored_channels,
            iu=ignored_users,
            im=ignored_mods,
            invites=invite_count,
        )
        for page in pagify(msg):
            await ctx.send(page, allowed_mentions=self.allowed_mentions)
