"""
cogs/admin.py — Admin slash commands for Enigma.

/game enable  <game>             — Enable a game in this guild
/game disable <game>             — Disable a game in this guild
/game channel <game> [channel]   — Set (or clear) the designated channel for a game
/game list                       — Show all games and their status
/game status  <game>             — Show config for one game
"""

import discord
from discord import app_commands
from discord.ext import commands
from config import GAME_REGISTRY
from database import get_db
from utils.scoring import info_embed, warn_embed, win_embed
import logging

log = logging.getLogger(__name__)

# ── Autocomplete helper ───────────────────────────────────────────────────────

async def game_key_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=name, value=key)
        for key, name in GAME_REGISTRY.items()
        if current.lower() in name.lower() or current.lower() in key.lower()
    ][:25]


# ── Cog ───────────────────────────────────────────────────────────────────────

class AdminCog(commands.Cog, name="Admin"):
    """Admin commands for configuring Enigma games per server."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    game_group = app_commands.Group(
        name="game",
        description="Configure Enigma games for this server.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    # ── /game enable ──────────────────────────────────────────────────────────

    @game_group.command(name="enable", description="Enable a game in this server.")
    @app_commands.describe(game="The game to enable.")
    @app_commands.autocomplete(game=game_key_autocomplete)
    async def game_enable(self, interaction: discord.Interaction, game: str):
        if game not in GAME_REGISTRY:
            await interaction.response.send_message(
                embed=warn_embed("Unknown game", f"`{game}` is not a valid game key."),
                ephemeral=True
            )
            return
        with get_db() as conn:
            conn.execute("""
                INSERT INTO guild_games (guild_id, game_key, enabled)
                VALUES (?, ?, 1)
                ON CONFLICT(guild_id, game_key)
                DO UPDATE SET enabled=1
            """, (interaction.guild_id, game))
        await interaction.response.send_message(
            embed=win_embed("Game enabled",
                            f"**{GAME_REGISTRY[game]}** is now enabled in this server."),
            ephemeral=True
        )

    # ── /game disable ─────────────────────────────────────────────────────────

    @game_group.command(name="disable", description="Disable a game in this server.")
    @app_commands.describe(game="The game to disable.")
    @app_commands.autocomplete(game=game_key_autocomplete)
    async def game_disable(self, interaction: discord.Interaction, game: str):
        if game not in GAME_REGISTRY:
            await interaction.response.send_message(
                embed=warn_embed("Unknown game", f"`{game}` is not a valid game key."),
                ephemeral=True
            )
            return
        with get_db() as conn:
            conn.execute("""
                INSERT INTO guild_games (guild_id, game_key, enabled)
                VALUES (?, ?, 0)
                ON CONFLICT(guild_id, game_key)
                DO UPDATE SET enabled=0
            """, (interaction.guild_id, game))
        await interaction.response.send_message(
            embed=warn_embed("Game disabled",
                             f"**{GAME_REGISTRY[game]}** has been disabled in this server."),
            ephemeral=True
        )

    # ── /game channel ─────────────────────────────────────────────────────────

    @game_group.command(
        name="channel",
        description="Set or clear the channel restriction for a game."
    )
    @app_commands.describe(
        game="The game to configure.",
        channel="The channel to restrict this game to. Leave blank to allow any channel."
    )
    @app_commands.autocomplete(game=game_key_autocomplete)
    async def game_channel(
        self,
        interaction: discord.Interaction,
        game: str,
        channel: discord.TextChannel | None = None,
    ):
        if game not in GAME_REGISTRY:
            await interaction.response.send_message(
                embed=warn_embed("Unknown game", f"`{game}` is not a valid game key."),
                ephemeral=True
            )
            return
        ch_id = channel.id if channel else None
        with get_db() as conn:
            conn.execute("""
                INSERT INTO guild_games (guild_id, game_key, enabled, channel_id)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(guild_id, game_key)
                DO UPDATE SET channel_id=excluded.channel_id
            """, (interaction.guild_id, game, ch_id))
        if channel:
            msg = f"**{GAME_REGISTRY[game]}** is now restricted to {channel.mention}."
        else:
            msg = f"**{GAME_REGISTRY[game]}** channel restriction cleared — any channel allowed."
        await interaction.response.send_message(
            embed=info_embed("Channel updated", msg),
            ephemeral=True
        )

    # ── /game list ────────────────────────────────────────────────────────────

    @game_group.command(name="list", description="List all games and their status in this server.")
    async def game_list(self, interaction: discord.Interaction):
        with get_db() as conn:
            rows = conn.execute(
                "SELECT game_key, enabled, channel_id FROM guild_games WHERE guild_id=?",
                (interaction.guild_id,)
            ).fetchall()

        config_map = {r["game_key"]: r for r in rows}
        lines = []
        for key, name in GAME_REGISTRY.items():
            row = config_map.get(key)
            if row and row["enabled"]:
                ch = f" → <#{row['channel_id']}>" if row["channel_id"] else ""
                lines.append(f"✅ **{name}**{ch}")
            else:
                lines.append(f"❌ **{name}**")

        pages = []
        chunk: list[str] = []
        for line in lines:
            chunk.append(line)
            if len(chunk) == 15:
                pages.append("\n".join(chunk))
                chunk = []
        if chunk:
            pages.append("\n".join(chunk))

        # Send first page (Discord embed limit means one page is fine for ≤35 games)
        embed = info_embed(
            f"Games — {interaction.guild.name}",
            pages[0] if pages else "*No games configured yet.*"
        )
        embed.set_footer(text="Use /game enable or /game disable to toggle games.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /game status ──────────────────────────────────────────────────────────

    @game_group.command(name="status", description="Check the config for a specific game.")
    @app_commands.describe(game="The game to inspect.")
    @app_commands.autocomplete(game=game_key_autocomplete)
    async def game_status(self, interaction: discord.Interaction, game: str):
        if game not in GAME_REGISTRY:
            await interaction.response.send_message(
                embed=warn_embed("Unknown game", f"`{game}` is not a valid game key."),
                ephemeral=True
            )
            return
        with get_db() as conn:
            row = conn.execute(
                "SELECT enabled, channel_id FROM guild_games WHERE guild_id=? AND game_key=?",
                (interaction.guild_id, game)
            ).fetchone()

        name = GAME_REGISTRY[game]
        if not row:
            desc = "Not configured (disabled by default)."
        else:
            status = "✅ Enabled" if row["enabled"] else "❌ Disabled"
            ch = f"<#{row['channel_id']}>" if row["channel_id"] else "Any channel"
            desc = f"**Status:** {status}\n**Channel:** {ch}"
        await interaction.response.send_message(
            embed=info_embed(f"Game status — {name}", desc),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
