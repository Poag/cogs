from .gamelog import GameLog


async def setup(bot):
    await bot.add_cog(GameLog(bot))
