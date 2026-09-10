from .voicelog import VoiceLog


async def setup(bot):
    await bot.add_cog(VoiceLog(bot))
