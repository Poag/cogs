from .example import Example


async def setup(bot):
    await bot.add_cog(Example(bot))
