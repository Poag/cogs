"""TimedRoles cog: grant or remove roles based on time spent on the server."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.commands import parse_timedelta
from redbot.core.utils import AsyncIter
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.timedroles")

TIMEROLE_CONFIG_IDENTIFIER = 9811198108111121
TIMEROLE_COG_NAME = "Timerole"


async def sleep_till_next_hour() -> None:
    """Sleep until the top of the next hour."""
    now = datetime.now(timezone.utc)
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    await asyncio.sleep((next_hour - datetime.now(timezone.utc)).total_seconds())


async def announce_to_channel(channel: Optional[discord.TextChannel], results: str, title: str) -> None:
    """Send a results summary to a channel, or log it if there's no channel configured."""
    if channel is not None and results:
        await channel.send(title)
        for page in pagify(results, shorten_by=50):
            await channel.send(page)
    elif results:
        log.info(results)


class TimedRoles(commands.Cog):
    """Grant or remove roles based on how long a member has been on the server.

    From-scratch reimplementation of fox-v3's `timerole` cog. Use
    `[p]timedroles migrate` to bring over an existing timerole setup.
    """

    def __init__(self, bot: Red) -> None:
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, identifier=193725104, force_registration=True)
        self.config.register_guild(
            announce=None,
            reapply=True,
            roles={},
            skipbots=True,
        )
        self.config.init_custom("RoleMember", 2)
        self.config.register_custom("RoleMember", had_role=False, check_again_time=None)
        self._update_task: Optional[asyncio.Task] = None

    def cog_unload(self) -> None:
        if self._update_task:
            self._update_task.cancel()

    async def cog_load(self) -> None:
        self._update_task = asyncio.create_task(self._check_hour_loop())

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Clear a user's per-role tracking state (had_role/check_again_time) everywhere."""
        all_role_member = await self.config.custom("RoleMember").all()
        for role_id in all_role_member:
            if str(user_id) in all_role_member[role_id]:
                await self.config.custom("RoleMember", role_id, str(user_id)).clear()

    #
    # Commands
    #

    @commands.command()
    @commands.guildowner()
    @commands.guild_only()
    async def runtimedroles(self, ctx: commands.Context) -> None:
        """Trigger the hourly timedroles check now.

        Useful for troubleshooting the initial setup.
        """
        async with ctx.typing():
            pre_run = datetime.now(timezone.utc)
            await self.timedroles_update()
            after_run = datetime.now(timezone.utc)
            await ctx.tick()
        await ctx.maybe_send_embed(f"Took {after_run - pre_run} seconds")

    @commands.group()
    @commands.mod_or_permissions(administrator=True)
    @commands.guild_only()
    async def timedroles(self, ctx: commands.Context) -> None:
        """Adjust timedroles settings."""

    @timedroles.command()
    async def addrole(
        self,
        ctx: commands.Context,
        role: discord.Role,
        time: str,
        *requiredroles: discord.Role,
    ) -> None:
        """Add a role to be added after the specified time on server."""
        if not ctx.guild:
            return
        try:
            parsed_time = parse_timedelta(time, allowed_units=["weeks", "days", "hours"])
        except commands.BadArgument:
            await ctx.maybe_send_embed("Error: Invalid time string.")
            return
        if parsed_time is None:
            await ctx.maybe_send_embed("Error: Invalid time string.")
            return

        days = parsed_time.days
        hours = parsed_time.seconds // 60 // 60
        to_set = {"days": days, "hours": hours, "remove": False}
        if requiredroles:
            to_set["required"] = [r.id for r in requiredroles]

        await self.config.guild(ctx.guild).roles.set_raw(role.id, value=to_set)
        await ctx.maybe_send_embed(
            f"Time Role for {role.name} set to {days} days and {hours} hours until added"
        )

    @timedroles.command()
    async def removerole(
        self,
        ctx: commands.Context,
        role: discord.Role,
        time: str,
        *requiredroles: discord.Role,
    ) -> None:
        """Add a role to be removed after the specified time on server."""
        if not ctx.guild:
            return
        try:
            parsed_time = parse_timedelta(time, allowed_units=["weeks", "days", "hours"])
        except commands.BadArgument:
            await ctx.maybe_send_embed("Error: Invalid time string.")
            return
        if parsed_time is None:
            await ctx.maybe_send_embed("Error: Invalid time string.")
            return

        days = parsed_time.days
        hours = parsed_time.seconds // 60 // 60
        to_set = {"days": days, "hours": hours, "remove": True}
        if requiredroles:
            to_set["required"] = [r.id for r in requiredroles]

        await self.config.guild(ctx.guild).roles.set_raw(role.id, value=to_set)
        await ctx.maybe_send_embed(
            f"Time Role for {role.name} set to {days} days and {hours} hours until removed"
        )

    @timedroles.command(name="channel")
    async def timedroles_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set the announce channel for role adds/removes."""
        if not ctx.guild:
            return
        if channel is None:
            await self.config.guild(ctx.guild).announce.clear()
            await ctx.maybe_send_embed("Announce channel has been cleared")
        else:
            await self.config.guild(ctx.guild).announce.set(channel.id)
            await ctx.send(f"Announce channel set to {channel.mention}")

    @timedroles.command()
    async def reapply(self, ctx: commands.Context) -> None:
        """Toggle reapplying roles if a member loses one somehow. Defaults to True."""
        if not ctx.guild:
            return
        current_setting = await self.config.guild(ctx.guild).reapply()
        await self.config.guild(ctx.guild).reapply.set(not current_setting)
        await ctx.maybe_send_embed(f"Reapplying roles is now set to: {not current_setting}")

    @timedroles.command()
    async def skipbots(self, ctx: commands.Context) -> None:
        """Toggle skipping bots when adding/removing roles. Defaults to True."""
        if not ctx.guild:
            return
        current_setting = await self.config.guild(ctx.guild).skipbots()
        await self.config.guild(ctx.guild).skipbots.set(not current_setting)
        await ctx.maybe_send_embed(f"Skipping bots is now set to: {not current_setting}")

    @timedroles.command()
    async def delrole(self, ctx: commands.Context, role: discord.Role) -> None:
        """Stop a role from being added/removed after a specified time."""
        if not ctx.guild:
            return
        await self.config.guild(ctx.guild).roles.set_raw(role.id, value=None)
        await self.config.custom("RoleMember", role.id).clear()
        await ctx.maybe_send_embed(f"{role.name} will no longer be applied")

    @timedroles.command(name="list")
    async def timedroles_list(self, ctx: commands.Context) -> None:
        """List all currently configured timedroles."""
        if not ctx.guild:
            return
        role_dict = await self.config.guild(ctx.guild).roles()
        out = "Current TimedRoles:\n"
        for r_id, r_data in role_dict.items():
            if r_data is None:
                continue
            role = discord.utils.get(ctx.guild.roles, id=int(r_id))
            r_roles = []
            if role is None:
                role = r_id
            if "required" in r_data:
                r_roles = [
                    str(discord.utils.get(ctx.guild.roles, id=int(new_id)))
                    for new_id in r_data["required"]
                ]
            out += f"{role} | {r_data['days']} days | requires: {r_roles}\n"
        await ctx.maybe_send_embed(out)

    #
    # Migration
    #

    @timedroles.command()
    async def migrate(self, ctx: commands.Context) -> None:
        """Migrate this server's timerole setup into TimedRoles.

        Reads timerole's own stored configuration directly (it does not
        need to be loaded). Copies over the announce channel, reapply/
        skipbots toggles, every configured role, and the per-member
        tracking state for those roles. Safe to run more than once.
        """
        if not ctx.guild:
            return

        old_config = Config.get_conf(
            cog_instance=None,
            identifier=TIMEROLE_CONFIG_IDENTIFIER,
            force_registration=False,
            cog_name=TIMEROLE_COG_NAME,
        )
        old_config.register_guild(
            announce=None,
            reapply=True,
            roles={},
            skipbots=True,
        )
        old_config.init_custom("RoleMember", 2)
        old_config.register_custom("RoleMember", had_role=False, check_again_time=None)

        old_guild_conf = old_config.guild(ctx.guild)
        await self.config.guild(ctx.guild).announce.set(await old_guild_conf.announce())
        await self.config.guild(ctx.guild).reapply.set(await old_guild_conf.reapply())
        await self.config.guild(ctx.guild).skipbots.set(await old_guild_conf.skipbots())
        old_roles = await old_guild_conf.roles()
        await self.config.guild(ctx.guild).roles.set(old_roles)

        member_states_migrated = 0
        for role_id, role_data in old_roles.items():
            if role_data is None:
                continue
            old_role_members = await old_config.custom("RoleMember", role_id).all()
            for member_id, state in old_role_members.items():
                await self.config.custom("RoleMember", role_id, member_id).set(state)
                member_states_migrated += 1

        await ctx.maybe_send_embed(
            f"Migrated {len([r for r in old_roles.values() if r is not None])} timedrole(s) "
            f"and {member_states_migrated} member tracking record(s) from timerole.\n\n"
            "Once you've confirmed TimedRoles is working correctly, run `[p]unload timerole` "
            "to fully retire the old cog. This command does not do that for you."
        )

    #
    # Update loop
    #

    async def timedroles_update(self) -> None:
        """Check every member in every guild against its configured timedroles."""
        utcnow = datetime.now(timezone.utc)
        all_guilds = await self.config.all_guilds()

        for guild in self.bot.guilds:
            guild_id = guild.id
            if guild_id not in all_guilds:
                continue

            add_results = ""
            remove_results = ""
            reapply = all_guilds[guild_id]["reapply"]
            role_dict = all_guilds[guild_id]["roles"]
            skipbots = all_guilds[guild_id]["skipbots"]

            if not any(role_dict.values()):
                continue

            async for member in AsyncIter(guild.members, steps=10):
                if member.bot and skipbots:
                    continue
                if member.joined_at is None:
                    continue

                addlist = []
                removelist = []

                for role_id, role_data in role_dict.items():
                    if not role_data:
                        continue

                    mr_dict = await self.config.custom("RoleMember", role_id, member.id).all()

                    if not reapply and mr_dict["had_role"]:
                        continue

                    if mr_dict["check_again_time"] is not None:
                        check_again_time = datetime.fromisoformat(mr_dict["check_again_time"])
                        if check_again_time.tzinfo is None:
                            check_again_time = check_again_time.replace(tzinfo=timezone.utc)
                        if check_again_time >= utcnow:
                            continue

                    has_roles = {r.id for r in member.roles}

                    if (int(role_id) in has_roles and not role_data["remove"]) or (
                        int(role_id) not in has_roles and role_data["remove"]
                    ):
                        if not mr_dict["had_role"]:
                            await self.config.custom(
                                "RoleMember", role_id, member.id
                            ).had_role.set(True)
                        continue

                    if "required" in role_data and not set(role_data["required"]) & has_roles:
                        continue

                    check_time = member.joined_at + timedelta(
                        days=role_data["days"],
                        hours=role_data.get("hours", 0),
                    )

                    if check_time >= utcnow:
                        await self.config.custom(
                            "RoleMember", role_id, member.id
                        ).check_again_time.set(check_time.isoformat())
                        continue

                    if role_data["remove"]:
                        removelist.append(role_id)
                    else:
                        addlist.append(role_id)

                if not addlist and not removelist:
                    continue

                add_roles = [discord.utils.get(guild.roles, id=int(rid)) for rid in addlist]
                remove_roles = [discord.utils.get(guild.roles, id=int(rid)) for rid in removelist]

                if addlist:
                    add_roles = [r for r in add_roles if r is not None]
                    try:
                        await member.add_roles(*add_roles, reason="TimedRoles", atomic=False)
                    except (discord.Forbidden, discord.NotFound):
                        log.exception("Failed adding roles to %s", member)
                        add_results += f"{member.display_name} : **(Failed Adding Roles)**\n"
                    else:
                        add_results += (
                            " \n".join(f"{member.display_name} : {r.name}" for r in add_roles)
                            + "\n"
                        )
                        for role_id in addlist:
                            await self.config.custom(
                                "RoleMember", role_id, member.id
                            ).had_role.set(True)

                if removelist:
                    remove_roles = [r for r in remove_roles if r is not None]
                    try:
                        await member.remove_roles(*remove_roles, reason="TimedRoles", atomic=False)
                    except (discord.Forbidden, discord.NotFound):
                        log.exception("Failed removing roles from %s", member)
                        remove_results += f"{member.display_name} : **(Failed Removing Roles)**\n"
                    else:
                        remove_results += (
                            " \n".join(f"{member.display_name} : {r.name}" for r in remove_roles)
                            + "\n"
                        )
                        for role_id in removelist:
                            await self.config.custom(
                                "RoleMember", role_id, member.id
                            ).had_role.set(True)

            channel_id = await self.config.guild(guild).announce()
            channel = guild.get_channel(channel_id) if channel_id is not None else None

            if add_results:
                await announce_to_channel(
                    channel, add_results, "**These members have received the following roles**\n"
                )
            if remove_results:
                await announce_to_channel(
                    channel, remove_results, "**These members have lost the following roles**\n"
                )

    async def _check_hour_loop(self) -> None:
        await self.bot.wait_until_ready()
        await sleep_till_next_hour()
        while self is self.bot.get_cog("TimedRoles"):
            await self.timedroles_update()
            await sleep_till_next_hour()
