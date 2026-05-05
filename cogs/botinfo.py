"""cogs/botinfo.py — /botinfo command showing bot system information."""

import platform
import socket
import time

import discord
import psutil
from discord import app_commands
from discord.ext import commands

COLOUR = discord.Colour.from_str("#5865F2")

_start_time = time.time()


def _fmt_uptime(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_bytes(b: int) -> str:
    gb = b / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.2f}GB"
    return f"{b / (1024 ** 2):.2f}MB"


class BotInfoCog(commands.Cog, name="BotInfo"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="botinfo", description="Display technical information about the bot.")
    async def botinfo(self, interaction: discord.Interaction):
        uname = platform.uname()
        os_str = f"{uname.system} {uname.release}"
        hostname = socket.gethostname()
        arch = platform.machine()
        cpu_cores = psutil.cpu_count(logical=False) or psutil.cpu_count()
        cpu_usage = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        py_version = platform.python_version()
        dpy_version = discord.__version__
        uptime = _fmt_uptime(time.time() - _start_time)

        guild_count = len(self.bot.guilds)
        channel_count = sum(len(g.channels) for g in self.bot.guilds)
        user_count = sum(g.member_count or 0 for g in self.bot.guilds)

        cmds = self.bot.tree.get_commands()
        total_commands = sum(
            1 + len([o for o in (c.options or []) if isinstance(o, app_commands.AppCommandGroup)])
            if hasattr(c, "commands")
            else 1
            for c in cmds
        )

        lines = [
            f"**Operating System**: {os_str}",
            f"**Uptime**: {uptime}",
            f"**Hostname**: {hostname}",
            f"**CPU Architecture**: {arch} ({cpu_cores} cores)",
            f"**CPU Usage**: {cpu_usage:.0f}%",
            f"**Memory Usage**: {_fmt_bytes(mem.used)} / {_fmt_bytes(mem.total)}",
            f"**Python Version**: v{py_version}",
            f"**Discord.py Version**: {dpy_version}",
            f"**Connected to** {guild_count} guilds, {channel_count} channels, and {user_count} users",
            f"**Total Commands**: {total_commands}",
        ]

        embed = discord.Embed(
            title="Bot Information:",
            description="\n".join(f"• {line}" for line in lines),
            colour=COLOUR,
        )

        await interaction.response.send_message(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(BotInfoCog(bot))
