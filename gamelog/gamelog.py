import asyncio
import datetime
import logging
import sqlite3
from typing import Dict, Optional, Tuple

import discord
from redbot.core import commands
from redbot.core.data_manager import cog_data_path
from redbot.core.utils.chat_formatting import humanize_timedelta

log = logging.getLogger("red.gamelog")


class GameLog(commands.Cog):
    """Logs who's playing what game in a server, and how long each session lasts.

    Only "Playing" activities count as games. Other presence types (Spotify,
    other listening/watching/streaming/custom statuses) are ignored.
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
        self._db.commit()
        self._db_lock = asyncio.Lock()
        # (guild_id, user_id, game) -> when that session started
        self._active: Dict[Tuple[int, int, str], datetime.datetime] = {}

    def cog_unload(self) -> None:
        self._db.close()

    async def cog_load(self) -> None:
        if not (self.bot.intents.presences and self.bot.intents.members):
            log.warning(
                "GameLog requires the Presence and Server Members privileged intents. "
                "Enable both in the Discord developer portal and in Red's intents "
                "settings, then restart the bot, or game activity will never be seen."
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
        """Show the top players of a game in this server by total logged time."""

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

        lines = []
        for rank, (user_id, total) in enumerate(rows, start=1):
            member = ctx.guild.get_member(user_id)
            name = member.display_name if member else f"<@{user_id}>"
            lines.append(f"{rank}. {name} — {humanize_timedelta(seconds=total)}")

        embed = discord.Embed(
            title=f"Top players: {game}",
            description="\n".join(lines),
            color=await ctx.embed_color(),
        )
        await ctx.send(embed=embed)

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        def _delete() -> None:
            self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self._db.commit()

        async with self._db_lock:
            await asyncio.get_running_loop().run_in_executor(None, _delete)
