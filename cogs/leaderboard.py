"""
cogs/leaderboard.py — All leaderboard slash commands for Enigma.

/leaderboard overall           — All-time points across every game
/leaderboard game <game>       — Points for a specific game
/leaderboard category <cat>    — Category specialist board
/leaderboard speed <game>      — Average solve time (lower = better)
/leaderboard stumper           — Whose questions stumped the most people
/leaderboard calibration       — Estimation game accuracy (Fermi / Probability / Timeline)
/leaderboard contributor       — Most questions / facts / clips submitted
/leaderboard streak            — Longest current correct-answer streak
"""

import discord
from discord import app_commands
from discord.ext import commands
from database import get_db
from config import GAME_REGISTRY
from utils.scoring import info_embed, format_leaderboard
import logging

log = logging.getLogger(__name__)


async def game_key_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=name, value=key)
        for key, name in GAME_REGISTRY.items()
        if current.lower() in name.lower()
    ][:25]


CATEGORY_LIST = [
    "Science & Nature", "History", "Geography", "Entertainment",
    "Sports", "Art & Literature", "Technology", "Mythology",
    "Politics", "Music", "Film & TV", "Food & Drink", "General Knowledge",
]


async def category_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=c, value=c)
        for c in CATEGORY_LIST if current.lower() in c.lower()
    ][:25]


class LeaderboardCog(commands.Cog, name="Leaderboard"):
    """Leaderboard commands for Enigma."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    lb = app_commands.Group(
        name="leaderboard",
        description="View Enigma leaderboards.",
        guild_only=True,
    )

    # ── /leaderboard overall ─────────────────────────────────────────────────

    @lb.command(name="overall", description="All-time points across every game.")
    async def lb_overall(self, interaction: discord.Interaction):
        with get_db() as conn:
            rows = conn.execute("""
                SELECT user_id, SUM(points) AS total
                FROM points WHERE guild_id=?
                GROUP BY user_id ORDER BY total DESC LIMIT 10
            """, (interaction.guild_id,)).fetchall()
        embed = format_leaderboard(rows, "pts", "🏆 All-Time Knowledge Board",
                                   interaction.guild)
        await interaction.response.send_message(embed=embed)

    # ── /leaderboard game ────────────────────────────────────────────────────

    @lb.command(name="game", description="Points leaderboard for a specific game.")
    @app_commands.describe(game="The game to view.")
    @app_commands.autocomplete(game=game_key_autocomplete)
    async def lb_game(self, interaction: discord.Interaction, game: str):
        if game not in GAME_REGISTRY:
            await interaction.response.send_message("Unknown game.", ephemeral=True)
            return
        with get_db() as conn:
            rows = conn.execute("""
                SELECT user_id, points FROM points
                WHERE guild_id=? AND game_key=?
                ORDER BY points DESC LIMIT 10
            """, (interaction.guild_id, game)).fetchall()
        name = GAME_REGISTRY[game]
        embed = format_leaderboard(rows, "pts", f"🎮 {name} — Leaderboard",
                                   interaction.guild)
        await interaction.response.send_message(embed=embed)

    # ── /leaderboard category ────────────────────────────────────────────────

    @lb.command(name="category", description="Category specialist leaderboard.")
    @app_commands.describe(category="The knowledge category.")
    @app_commands.autocomplete(category=category_autocomplete)
    async def lb_category(self, interaction: discord.Interaction, category: str):
        with get_db() as conn:
            rows = conn.execute("""
                SELECT user_id, points FROM category_points
                WHERE guild_id=? AND category=?
                ORDER BY points DESC LIMIT 10
            """, (interaction.guild_id, category)).fetchall()
        embed = format_leaderboard(rows, "pts", f"👑 {category} — Category King",
                                   interaction.guild)
        await interaction.response.send_message(embed=embed)

    # ── /leaderboard speed ───────────────────────────────────────────────────

    @lb.command(name="speed", description="Average solve time — lower is better.")
    @app_commands.describe(game="Game to view speed stats for (leave blank for overall).")
    @app_commands.autocomplete(game=game_key_autocomplete)
    async def lb_speed(self, interaction: discord.Interaction, game: str | None = None):
        with get_db() as conn:
            if game:
                rows = conn.execute("""
                    SELECT user_id, CAST(AVG(solve_ms) AS INTEGER) AS avg_ms
                    FROM solve_times WHERE guild_id=? AND game_key=?
                    GROUP BY user_id HAVING COUNT(*) >= 3
                    ORDER BY avg_ms ASC LIMIT 10
                """, (interaction.guild_id, game)).fetchall()
                title = f"⚡ Speed Board — {GAME_REGISTRY.get(game, game)}"
            else:
                rows = conn.execute("""
                    SELECT user_id, CAST(AVG(solve_ms) AS INTEGER) AS avg_ms
                    FROM solve_times WHERE guild_id=?
                    GROUP BY user_id HAVING COUNT(*) >= 5
                    ORDER BY avg_ms ASC LIMIT 10
                """, (interaction.guild_id,)).fetchall()
                title = "⚡ Speed Board — Overall"

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(rows):
            uid = row["user_id"]
            ms = row["avg_ms"]
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            secs = ms / 1000
            member = interaction.guild.get_member(uid)
            name_str = member.display_name if member else f"<@{uid}>"
            lines.append(f"{medal} **{name_str}** — avg {secs:.1f}s")

        desc = "\n".join(lines) if lines else "*Not enough data yet (min 3 solves required).*"
        await interaction.response.send_message(
            embed=info_embed(title, desc)
        )

    # ── /leaderboard stumper ─────────────────────────────────────────────────

    @lb.command(name="stumper", description="Whose submitted questions stumped the most people.")
    async def lb_stumper(self, interaction: discord.Interaction):
        with get_db() as conn:
            rows = conn.execute("""
                SELECT author_id AS user_id, SUM(times_stumped) AS total
                FROM question_pool WHERE guild_id=? AND approved=1
                GROUP BY author_id ORDER BY total DESC LIMIT 10
            """, (interaction.guild_id,)).fetchall()
        embed = format_leaderboard(rows, "stumps", "🧠 Stumper Board", interaction.guild)
        await interaction.response.send_message(embed=embed)

    # ── /leaderboard calibration ─────────────────────────────────────────────

    @lb.command(
        name="calibration",
        description="Estimation accuracy over time (Fermi / Probability / Timeline)."
    )
    async def lb_calibration(self, interaction: discord.Interaction):
        estimation_keys = ("fermi_estimator", "probability_pulse", "timeline_toss", "price_is_right")
        placeholders = ",".join("?" * len(estimation_keys))
        with get_db() as conn:
            rows = conn.execute(f"""
                SELECT user_id, SUM(points) AS total
                FROM points
                WHERE guild_id=? AND game_key IN ({placeholders})
                GROUP BY user_id ORDER BY total DESC LIMIT 10
            """, (interaction.guild_id, *estimation_keys)).fetchall()
        embed = format_leaderboard(rows, "pts", "🎯 Calibration Board", interaction.guild)
        embed.set_footer(text="Tracks cumulative accuracy across Fermi, Probability Pulse, Price Is Right, and Timeline Toss.")
        await interaction.response.send_message(embed=embed)

    # ── /leaderboard contributor ─────────────────────────────────────────────

    @lb.command(name="contributor", description="Members who contribute the most content.")
    async def lb_contributor(self, interaction: discord.Interaction):
        with get_db() as conn:
            rows = conn.execute("""
                SELECT user_id,
                       (questions_submitted + facts_submitted + clips_submitted) AS total
                FROM contributions WHERE guild_id=?
                ORDER BY total DESC LIMIT 10
            """, (interaction.guild_id,)).fetchall()
        embed = format_leaderboard(rows, "contributions", "📝 Contributor Board",
                                   interaction.guild)
        await interaction.response.send_message(embed=embed)

    # ── /leaderboard streak ──────────────────────────────────────────────────

    @lb.command(name="streak", description="Longest current correct-answer streaks.")
    async def lb_streak(self, interaction: discord.Interaction):
        with get_db() as conn:
            rows = conn.execute("""
                SELECT user_id, current_streak AS value
                FROM streaks WHERE guild_id=?
                ORDER BY current_streak DESC LIMIT 10
            """, (interaction.guild_id,)).fetchall()

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(rows):
            uid = row["user_id"]
            streak = row["value"]
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            member = interaction.guild.get_member(uid)
            name_str = member.display_name if member else f"<@{uid}>"
            fire = "🔥" * min(streak // 5, 5)
            lines.append(f"{medal} **{name_str}** — {streak} {fire}")

        desc = "\n".join(lines) if lines else "*No streaks yet.*"
        await interaction.response.send_message(
            embed=info_embed("🔥 Hot Streak Board", desc)
        )

    # ── /leaderboard best_streak ─────────────────────────────────────────────

    @lb.command(name="best_streak", description="All-time best correct-answer streaks.")
    async def lb_best_streak(self, interaction: discord.Interaction):
        with get_db() as conn:
            rows = conn.execute("""
                SELECT user_id, best_streak AS value
                FROM streaks WHERE guild_id=?
                ORDER BY best_streak DESC LIMIT 10
            """, (interaction.guild_id,)).fetchall()

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(rows):
            uid = row["user_id"]
            streak = row["value"]
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            member = interaction.guild.get_member(uid)
            name_str = member.display_name if member else f"<@{uid}>"
            lines.append(f"{medal} **{name_str}** — {streak} best")

        desc = "\n".join(lines) if lines else "*No streaks yet.*"
        await interaction.response.send_message(
            embed=info_embed("🏅 Best Streak Board", desc)
        )

    # ── /leaderboard me ──────────────────────────────────────────────────────

    @lb.command(name="me", description="Your personal stats summary.")
    async def lb_me(self, interaction: discord.Interaction):
        uid = interaction.user.id
        gid = interaction.guild_id

        with get_db() as conn:
            total = conn.execute(
                "SELECT COALESCE(SUM(points),0) FROM points WHERE guild_id=? AND user_id=?",
                (gid, uid)
            ).fetchone()[0]

            streak_row = conn.execute(
                "SELECT current_streak, best_streak FROM streaks WHERE guild_id=? AND user_id=?",
                (gid, uid)
            ).fetchone()

            solves = conn.execute(
                "SELECT COUNT(*) FROM solve_times WHERE guild_id=? AND user_id=?",
                (gid, uid)
            ).fetchone()[0]

            avg_ms_row = conn.execute(
                "SELECT AVG(solve_ms) FROM solve_times WHERE guild_id=? AND user_id=?",
                (gid, uid)
            ).fetchone()

            top_cat = conn.execute("""
                SELECT category, points FROM category_points
                WHERE guild_id=? AND user_id=? ORDER BY points DESC LIMIT 1
            """, (gid, uid)).fetchone()

            contrib = conn.execute(
                "SELECT * FROM contributions WHERE guild_id=? AND user_id=?",
                (gid, uid)
            ).fetchone()

        cur_streak = streak_row["current_streak"] if streak_row else 0
        best_streak = streak_row["best_streak"] if streak_row else 0
        avg_s = f"{avg_ms_row[0]/1000:.1f}s" if avg_ms_row and avg_ms_row[0] else "N/A"

        lines = [
            f"**Total points:** {total:,}",
            f"**Total solves:** {solves:,}",
            f"**Avg solve time:** {avg_s}",
            f"**Current streak:** {cur_streak} 🔥",
            f"**Best streak:** {best_streak}",
        ]
        if top_cat:
            lines.append(f"**Top category:** {top_cat['category']} ({top_cat['points']} pts)")
        if contrib:
            total_contrib = (contrib["questions_submitted"]
                             + contrib["facts_submitted"]
                             + contrib["clips_submitted"])
            lines.append(f"**Contributions:** {total_contrib}")

        embed = info_embed(
            f"📊 {interaction.user.display_name}'s Stats",
            "\n".join(lines)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
