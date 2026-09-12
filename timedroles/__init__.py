from .timedroles import TimedRoles


async def setup(bot):
    cog = TimedRoles(bot)
    await bot.add_cog(cog)
