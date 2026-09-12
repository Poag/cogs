from .auditlog import AuditLog


async def setup(bot):
    await bot.add_cog(AuditLog(bot))
