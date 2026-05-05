"""
bot.py — Enigma Bot entry point
Initialises the bot, loads all cogs, syncs slash commands, and starts the scheduler.
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from config import BOT_TOKEN
from database import init_db

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("enigma")

# ─────────────────────────────────────────────
# Cog extensions to load
# ─────────────────────────────────────────────

EXTENSIONS = [
    "cogs.admin",
    "cogs.help",
    "cogs.leaderboard",
    "cogs.trivia.classic",
    "cogs.specialist.specialist",
    "cogs.wordplay.wordplay",
    "cogs.logic.logic",
    "cogs.visual.visual",
    "cogs.member.member",
]

# ─────────────────────────────────────────────
# Bot setup
# ─────────────────────────────────────────────

class EnigmaBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True   # needed for message-content scanning (Forbidden Word)
        intents.members = True           # needed to fetch member objects reliably

        super().__init__(
            command_prefix="!",          # legacy prefix (unused — all commands are slash)
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        log.info("Initialising database…")
        init_db()

        log.info("Loading extensions…")
        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
                log.info(f"  ✓ {ext}")
            except Exception as e:
                log.error(f"  ✗ {ext}: {e}", exc_info=True)

        log.info("Syncing slash commands globally (this may take up to an hour to propagate)…")
        synced = await self.tree.sync()
        log.info(f"  Synced {len(synced)} command(s).")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        log.info(f"Serving {len(self.guilds)} guild(s).")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name="Trivia & Puzzles | /game list",
            )
        )

    async def on_command_error(self, ctx, error):
        # Suppress "command not found" noise — we use slash commands
        if isinstance(error, commands.CommandNotFound):
            return
        log.error(f"Command error: {error}", exc_info=True)

    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception):
        msg = str(error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"⚠️ {msg}", ephemeral=True)
            else:
                await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)
        except Exception:
            pass
        log.error(f"App command error in /{interaction.command}: {error}", exc_info=True)


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

async def main():
    load_dotenv()
    token = os.getenv("BOT_TOKEN") or BOT_TOKEN
    if not token:
        raise ValueError("BOT_TOKEN is not set. Add it to your .env file.")

    bot = EnigmaBot()
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
