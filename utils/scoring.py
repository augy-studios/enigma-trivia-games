"""
utils/scoring.py — Shared helpers for scoring, embeds, and game guards.
"""

import discord
import logging
from database import (
    add_points, add_category_points, record_solve_time,
    update_streak, is_game_enabled, get_game_config,
)
from config import POINTS

log = logging.getLogger(__name__)

# ── Colour palette ────────────────────────────────────────────────────────────
COLOUR_WIN     = discord.Colour.from_str("#57F287")   # green
COLOUR_LOSS    = discord.Colour.from_str("#ED4245")   # red
COLOUR_INFO    = discord.Colour.from_str("#5865F2")   # blurple
COLOUR_WARN    = discord.Colour.from_str("#FEE75C")   # yellow
COLOUR_NEUTRAL = discord.Colour.from_str("#2B2D31")   # dark


def make_embed(title: str, description: str = "",
               colour: discord.Colour = COLOUR_INFO,
               footer: str = "") -> discord.Embed:
    embed = discord.Embed(title=title, description=description, colour=colour)
    if footer:
        embed.set_footer(text=footer)
    return embed


def win_embed(title: str, description: str = "", footer: str = "") -> discord.Embed:
    return make_embed(title, description, COLOUR_WIN, footer)


def info_embed(title: str, description: str = "", footer: str = "") -> discord.Embed:
    return make_embed(title, description, COLOUR_INFO, footer)


def warn_embed(title: str, description: str = "", footer: str = "") -> discord.Embed:
    return make_embed(title, description, COLOUR_WARN, footer)


# ── Game guard decorator ──────────────────────────────────────────────────────

def game_check(game_key: str):
    """
    Slash-command check: game must be enabled in this guild, and the
    interaction channel must match the configured channel (if set).
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message(
                embed=warn_embed("Server only", "This command can only be used in a server."),
                ephemeral=True
            )
            return False
        if not is_game_enabled(guild_id, game_key):
            await interaction.response.send_message(
                embed=warn_embed("Game disabled",
                                 f"**{game_key}** is not enabled on this server.\n"
                                 "An admin can enable it with `/game enable`."),
                ephemeral=True
            )
            return False
        config = get_game_config(guild_id, game_key)
        if config and config["channel_id"]:
            if interaction.channel_id != config["channel_id"]:
                ch = f"<#{config['channel_id']}>"
                await interaction.response.send_message(
                    embed=warn_embed("Wrong channel",
                                     f"This game is restricted to {ch}."),
                    ephemeral=True
                )
                return False
        return True
    return discord.app_commands.check(predicate)


# ── Award helper ─────────────────────────────────────────────────────────────

def award(guild_id: int, user_id: int, game_key: str, amount: int,
          category: str | None = None, solve_ms: int | None = None):
    """Add points, optionally add category points, optionally record solve time."""
    add_points(guild_id, user_id, game_key, amount)
    if category:
        add_category_points(guild_id, user_id, category, amount)
    if solve_ms is not None:
        record_solve_time(guild_id, user_id, game_key, solve_ms)
    update_streak(guild_id, user_id, correct=True)


# ── Answer normalisation ─────────────────────────────────────────────────────

def normalise(text: str) -> str:
    """Lowercase, strip punctuation/whitespace for fuzzy matching."""
    import re
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def answers_match(submitted: str, correct: str, threshold: float = 0.85) -> bool:
    """Return True if submitted answer is close enough to correct answer."""
    from difflib import SequenceMatcher
    s = normalise(submitted)
    c = normalise(correct)
    if s == c:
        return True
    ratio = SequenceMatcher(None, s, c).ratio()
    return ratio >= threshold


# ── Leaderboard formatter ─────────────────────────────────────────────────────

def format_leaderboard(rows: list, value_label: str = "pts",
                        title: str = "Leaderboard",
                        guild: discord.Guild | None = None) -> discord.Embed:
    """
    rows: list of sqlite3.Row with columns (user_id, value).
    Returns a formatted embed.
    """
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows[:10]):
        uid = row["user_id"] if "user_id" in row.keys() else row[0]
        val = row[1] if len(row.keys()) > 1 else row["points"]
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        if guild:
            member = guild.get_member(uid)
            name = member.display_name if member else f"<@{uid}>"
        else:
            name = f"<@{uid}>"
        lines.append(f"{medal} **{name}** — {val:,} {value_label}")
    description = "\n".join(lines) if lines else "*No entries yet.*"
    return info_embed(title, description)
