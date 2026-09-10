import asyncio
import datetime
import logging
import sqlite3
from typing import Dict, Optional, Tuple

import discord
from redbot.core import commands
from redbot.core.data_manager import cog_data_path
from redbot.core.utils.chat_formatting import box, humanize_timedelta, pagify

log = logging.getLogger("red.gamelog")


class GameLog(commands.Cog):
    """Logs who's playing what game and who's in voice channels, in a server.

    Only "Playing" activities count as games. Other presence types (Spotify,
    other listening/watching/streaming/custom statuses) are ignored.

    Game sessions and voice sessions are both logged as plain start/end
    intervals. Who was in voice "with" whom, and what game someone was
    playing during a given voice session, aren't computed by this cog -
    they can both be derived later by joining ``voice_sessions`` on
    overlapping ``channel_id``/time ranges, and joining against ``sessions``
    (the game log) on ``user_id``/overlapping time ranges. That join is the
    basis for building a relationship graph from this data.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        db_path = cog_data_path(self) / "gamelog.sqlite3"
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                start_time INTEGER NOT NULL,
                end_time INTEGER NOT NULL,
                duration INTEGER NOT NULL
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_guild_user_game "
            "ON sessions (guild_id, user_id, game)"
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                start_time INTEGER NOT NULL,
                end_time INTEGER NOT NULL,
                duration INTEGER NOT NULL
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_voice_sessions_guild_user "
            "ON voice_sessions (guild_id, user_id)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_voice_sessions_guild_channel_time "
            "ON voice_sessions (guild_id, channel_id, start_time, end_time)"
        )
        self._db.commit()
        self._db_lock = asyncio.Lock()
        # (guild_id, user_id, game) -> when that session started
        self._active: Dict[Tuple[int, int, str], datetime.datetime] = {}
        # (guild_id, user_id) -> (channel_id, when that voice session started)
        self._voice_active: Dict[Tuple[int, int], Tuple[int, datetime.datetime]] = {}

    def cog_unload(self) -> None:
        self._db.close()

    async def cog_load(self) -> None:
        if not (self.bot.intents.presences and self.bot.intents.members):
            log.warning(
                "GameLog requires the Presence and Server Members privileged intents. "
                "Enable both in the Discord developer portal and in Red's intents "
                "settings, then restart the bot, or game activity will never be seen."
            )
        if not self.bot.intents.voice_states:
            log.warning(
                "GameLog requires the Voice States intent to log voice channel "
                "activity. Enable it in Red's intents settings, then restart the bot."
            )

    @staticmethod
    def _playing_games(member: discord.Member) -> set:
        return {
            activity.name
            for activity in member.activities
            if activity.type is discord.ActivityType.playing and activity.name
        }

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot:
            return

        before_games = self._playing_games(before)
        after_games = self._playing_games(after)
        if before_games == after_games:
            return

        now = discord.utils.utcnow()

        for game in after_games - before_games:
            self._active[(after.guild.id, after.id, game)] = now

        for game in before_games - after_games:
            key = (after.guild.id, after.id, game)
            start = self._active.pop(key, None)
            if start is None:
                # We never saw this session start (e.g. the bot restarted
                # mid-session), so there's nothing accurate to log.
                continue
            duration = int((now - start).total_seconds())
            if duration <= 0:
                continue
            await self._log_session(after.guild.id, after.id, game, start, now, duration)

    async def _log_session(
        self,
        guild_id: int,
        user_id: int,
        game: str,
        start: datetime.datetime,
        end: datetime.datetime,
        duration: int,
    ) -> None:
        def _insert() -> None:
            self._db.execute(
                "INSERT INTO sessions (guild_id, user_id, game, start_time, end_time, duration) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, user_id, game, int(start.timestamp()), int(end.timestamp()), duration),
            )
            self._db.commit()

        async with self._db_lock:
            await asyncio.get_running_loop().run_in_executor(None, _insert)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if member.bot or before.channel == after.channel:
            return

        now = discord.utils.utcnow()
        key = (member.guild.id, member.id)

        if before.channel is not None:
            entry = self._voice_active.pop(key, None)
            if entry is not None:
                channel_id, start = entry
                duration = int((now - start).total_seconds())
                if duration > 0:
                    await self._log_voice_session(
                        member.guild.id, member.id, channel_id, start, now, duration
                    )

        if after.channel is not None:
            self._voice_active[key] = (after.channel.id, now)

    async def _log_voice_session(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        start: datetime.datetime,
        end: datetime.datetime,
        duration: int,
    ) -> None:
        def _insert() -> None:
            self._db.execute(
                "INSERT INTO voice_sessions "
                "(guild_id, user_id, channel_id, start_time, end_time, duration) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, user_id, channel_id, int(start.timestamp()), int(end.timestamp()), duration),
            )
            self._db.commit()

        async with self._db_lock:
            await asyncio.get_running_loop().run_in_executor(None, _insert)

    @commands.guild_only()
    @commands.group()
    async def gamelog(self, ctx: commands.Context) -> None:
        """View logged game activity for this server."""

    @gamelog.command(name="playtime")
    async def gamelog_playtime(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Show total logged time per game for a member (yourself by default)."""
        member = member or ctx.author

        def _query():
            cur = self._db.execute(
                "SELECT game, SUM(duration) FROM sessions "
                "WHERE guild_id = ? AND user_id = ? "
                "GROUP BY game ORDER BY SUM(duration) DESC",
                (ctx.guild.id, member.id),
            )
            return cur.fetchall()

        async with self._db_lock:
            rows = await asyncio.get_running_loop().run_in_executor(None, _query)

        if not rows:
            await ctx.send(f"No logged game activity for {member.display_name} in this server yet.")
            return

        lines = [f"{game}: {humanize_timedelta(seconds=total)}" for game, total in rows]
        embed = discord.Embed(
            title=f"Playtime for {member.display_name}",
            description="\n".join(lines),
            color=await ctx.embed_color(),
        )
        await ctx.send(embed=embed)

    @gamelog.command(name="top")
    async def gamelog_top(self, ctx: commands.Context, *, game: str) -> None:
        """Show the top 10 players of a game in this server by total logged time."""

        def _query():
            cur = self._db.execute(
                "SELECT user_id, SUM(duration) FROM sessions "
                "WHERE guild_id = ? AND game = ? "
                "GROUP BY user_id ORDER BY SUM(duration) DESC LIMIT 10",
                (ctx.guild.id, game),
            )
            return cur.fetchall()

        async with self._db_lock:
            rows = await asyncio.get_running_loop().run_in_executor(None, _query)

        if not rows:
            await ctx.send(f"No logged activity for **{game}** in this server yet.")
            return

        embed = discord.Embed(
            title=f"Top players: {game}",
            description=self._format_leaderboard(ctx, rows),
            color=await ctx.embed_color(),
        )
        await ctx.send(embed=embed)

    @gamelog.command(name="leaderboard")
    async def gamelog_leaderboard(self, ctx: commands.Context) -> None:
        """Show the top 10 players in this server by total logged time, across all games."""

        def _query():
            cur = self._db.execute(
                "SELECT user_id, SUM(duration) FROM sessions "
                "WHERE guild_id = ? GROUP BY user_id ORDER BY SUM(duration) DESC LIMIT 10",
                (ctx.guild.id,),
            )
            return cur.fetchall()

        async with self._db_lock:
            rows = await asyncio.get_running_loop().run_in_executor(None, _query)

        if not rows:
            await ctx.send("No game activity has been logged in this server yet.")
            return

        embed = discord.Embed(
            title="Top players (all games)",
            description=self._format_leaderboard(ctx, rows),
            color=await ctx.embed_color(),
        )
        await ctx.send(embed=embed)

    @gamelog.command(name="games")
    async def gamelog_games(self, ctx: commands.Context) -> None:
        """List every game logged in this server, sorted by total time played."""

        def _query():
            cur = self._db.execute(
                "SELECT game, SUM(duration), COUNT(DISTINCT user_id) FROM sessions "
                "WHERE guild_id = ? GROUP BY game ORDER BY SUM(duration) DESC",
                (ctx.guild.id,),
            )
            return cur.fetchall()

        async with self._db_lock:
            rows = await asyncio.get_running_loop().run_in_executor(None, _query)

        if not rows:
            await ctx.send("No game activity has been logged in this server yet.")
            return

        lines = [
            f"{game} — {humanize_timedelta(seconds=total)} "
            f"({players} player{'s' if players != 1 else ''})"
            for game, total, players in rows
        ]
        for page in pagify("\n".join(lines)):
            await ctx.send(box(page))

    @gamelog.command(name="voicetime")
    async def gamelog_voicetime(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Show total logged voice channel time for a member (yourself by default)."""
        member = member or ctx.author

        def _query():
            cur = self._db.execute(
                "SELECT channel_id, SUM(duration) FROM voice_sessions "
                "WHERE guild_id = ? AND user_id = ? "
                "GROUP BY channel_id ORDER BY SUM(duration) DESC",
                (ctx.guild.id, member.id),
            )
            return cur.fetchall()

        async with self._db_lock:
            rows = await asyncio.get_running_loop().run_in_executor(None, _query)

        if not rows:
            await ctx.send(f"No logged voice activity for {member.display_name} in this server yet.")
            return

        lines = []
        for channel_id, total in rows:
            channel = ctx.guild.get_channel(channel_id)
            name = channel.name if channel else f"deleted-channel-{channel_id}"
            lines.append(f"#{name}: {humanize_timedelta(seconds=total)}")

        embed = discord.Embed(
            title=f"Voice time for {member.display_name}",
            description="\n".join(lines),
            color=await ctx.embed_color(),
        )
        await ctx.send(embed=embed)

    @staticmethod
    def _format_leaderboard(ctx: commands.Context, rows) -> str:
        lines = []
        for rank, (user_id, total) in enumerate(rows, start=1):
            member = ctx.guild.get_member(user_id)
            name = member.display_name if member else f"<@{user_id}>"
            lines.append(f"{rank}. {name} — {humanize_timedelta(seconds=total)}")
        return "\n".join(lines)

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        def _delete() -> None:
            self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self._db.execute("DELETE FROM voice_sessions WHERE user_id = ?", (user_id,))
            self._db.commit()

        async with self._db_lock:
            await asyncio.get_running_loop().run_in_executor(None, _delete)
