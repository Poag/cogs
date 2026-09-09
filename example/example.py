from redbot.core import commands


class Example(commands.Cog):
    """An example cog demonstrating the standard Red cog layout."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def hello(self, ctx):
        """Say hello."""
        await ctx.send("Hello from the example cog!")
