"""Argument converters for the auditlog cog's commands."""

from discord.ext.commands.converter import Converter
from discord.ext.commands.errors import BadArgument
from redbot.core import commands
from redbot.core.utils.chat_formatting import humanize_list

#: Every event key that lives directly under a guild's Config entry and has
#: an "enabled" sub-key. Kept in one place so EventChooser and the `settings`
#: display can't drift apart.
EVENT_OPTIONS = [
    "message_edit",
    "message_delete",
    "user_change",
    "role_change",
    "role_create",
    "role_delete",
    "voice_change",
    "user_join",
    "user_left",
    "channel_change",
    "channel_create",
    "channel_delete",
    "guild_change",
    "emoji_change",
    "stickers_change",
    "commands_used",
    "invite_created",
    "invite_deleted",
    "thread_create",
    "thread_delete",
    "thread_change",
]


class CommandPrivs(Converter):
    """Converter for the privilege levels `[p]auditlog commandlevel` accepts."""

    async def convert(self, ctx: commands.Context, argument: str) -> str:
        levels = ["MOD", "ADMIN", "BOT_OWNER", "GUILD_OWNER", "NONE"]
        result = None
        if argument.upper() in levels:
            result = argument.upper()
        if argument == "all":
            result = "NONE"
        if not result:
            raise BadArgument(f"`{argument}` is not an available command permission.")
        return result


class EventChooser(Converter):
    """Converter matching one of the loggable event keys, case-insensitively.

    A `member_`-prefixed argument (e.g. `member_change`) is remapped to the
    matching `user_`-prefixed event key, since the user-facing commands and
    help text talk about "member" changes while the Config keys (kept 1:1
    with ExtendedModLog for migration) use "user_change" internally.
    """

    async def convert(self, ctx: commands.Context, argument: str) -> str:
        options = EVENT_OPTIONS
        result = None
        if argument.startswith("member_"):
            argument = argument.replace("member_", "user_", 1)
        if argument.lower() in options:
            result = argument.lower()
        if not result:
            raise BadArgument(
                "`{arg}` is not an available event option. Please choose from {options}.".format(
                    arg=argument, options=humanize_list([f"`{i}`" for i in options])
                )
            )
        return result
