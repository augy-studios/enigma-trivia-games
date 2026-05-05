"""
cogs/visual/visual.py — Visual & Media games for Enigma.

All image-based games require moderators to attach image URLs manually
(Discord bots cannot generate images). The bot posts the URL + embed;
members identify the subject.

  blurred_vision      — Pixelated image revealed every 30 min; fewer reveals = more pts.
  macro_world         — Extreme close-up; first ID wins.
  map_surgeon         — Cropped unlabelled map section; first ID wins.
  silhouette_showdown — Silhouette; difficulty escalates with collective accuracy.
  colour_coded        — Hex palette of a logo/flag/artwork; first ID wins.
  logo_blitz          — Cropped/modified logo; first ID wins.
  opening_line        — First line of book/film/game/song; first ID wins.
  character_silhouette— Fictional character silhouette; first ID + source wins.
"""

import discord
import json
import time
import logging
from datetime import datetime, timezone, timedelta

from discord import app_commands
from discord.ext import commands, tasks

from config import POINTS, TIMING
from database import (
    get_db, get_active_round, create_active_round, close_active_round,
    add_points, schedule_post, get_pending_schedules, mark_schedule_fired,
)
from utils.scoring import (
    game_check, award, answers_match, info_embed, win_embed, warn_embed,
)

log = logging.getLogger(__name__)


def _state(row) -> dict:
    return json.loads(row["state"])


def _save_state(round_id: int, state: dict):
    with get_db() as conn:
        conn.execute("UPDATE active_rounds SET state=? WHERE id=?",
                     (json.dumps(state), round_id))


def _future_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _image_embed(title: str, description: str, image_url: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=description,
                          colour=discord.Colour.from_str("#5865F2"))
    embed.set_image(url=image_url)
    return embed


class VisualCog(commands.Cog, name="Visual"):
    """Visual & Media identification games for Enigma."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.blurred_reveal_loop.start()

    def cog_unload(self):
        self.blurred_reveal_loop.cancel()

    @tasks.loop(seconds=120)
    async def blurred_reveal_loop(self):
        now = datetime.now(timezone.utc).isoformat()
        pending = get_pending_schedules(now)
        for sched in pending:
            if sched["game_key"] == "blurred_vision":
                try:
                    await self._drop_blurred_reveal(sched)
                except Exception as e:
                    log.error("blurred_reveal_loop error %s: %s", sched["id"], e)
                mark_schedule_fired(sched["id"])

    @blurred_reveal_loop.before_loop
    async def before_blurred(self):
        await self.bot.wait_until_ready()

    async def _drop_blurred_reveal(self, sched):
        payload = json.loads(sched["payload"] or "{}")
        rid = payload.get("round_id")
        reveal_index = payload.get("reveal_index", 0)
        with get_db() as conn:
            row = conn.execute("SELECT * FROM active_rounds WHERE id=?", (rid,)).fetchone()
        if not row:
            return
        state = _state(row)
        urls = state.get("reveal_urls", [])
        if reveal_index >= len(urls):
            return
        state["reveals_shown"] = reveal_index + 1
        _save_state(rid, state)
        channel = self.bot.get_channel(row["channel_id"])
        if channel:
            pts_left = max(POINTS["blurred_max"] - (reveal_index + 1) * POINTS["blurred_decay"], 1)
            embed = _image_embed(
                f"👁️ Blurred Vision — Reveal {reveal_index + 1}",
                f"*Getting clearer… Max points now: **{pts_left}***\n"
                f"Use `/blurred answer` to identify it!",
                urls[reveal_index]
            )
            await channel.send(embed=embed)
            # Schedule next reveal if more left
            if reveal_index + 1 < len(urls):
                schedule_post(
                    sched["guild_id"], "blurred_vision",
                    _future_iso(TIMING["blurred_reveal_interval"]),
                    json.dumps({"round_id": rid, "reveal_index": reveal_index + 1})
                )

    # ══════════════════════════════════════════════════════════════════════════
    # BLURRED VISION
    # ══════════════════════════════════════════════════════════════════════════

    blurred_group = app_commands.Group(
        name="blurred",
        description="Blurred Vision commands.",
        guild_only=True,
    )

    @blurred_group.command(name="post", description="[Mod] Post a Blurred Vision round.")
    @app_commands.describe(
        answer="The correct identification.",
        initial_url="URL of the most blurred/pixelated image.",
        reveal_url_2="URL of second (clearer) image.",
        reveal_url_3="URL of third (clearest) image.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @game_check("blurred_vision")
    async def blurred_post(self, interaction: discord.Interaction,
                           answer: str, initial_url: str,
                           reveal_url_2: str = "", reveal_url_3: str = ""):
        gid = interaction.guild_id
        if get_active_round(gid, "blurred_vision"):
            await interaction.response.send_message(
                embed=warn_embed("Already active", "A Blurred Vision round is live."),
                ephemeral=True
            )
            return
        reveal_urls = [u for u in [reveal_url_2, reveal_url_3] if u]
        state = {
            "answer": answer,
            "reveal_urls": reveal_urls,
            "reveals_shown": 0,
            "answered_by": [],
            "started_ts": time.time(),
        }
        rid = create_active_round(gid, "blurred_vision", interaction.channel_id, json.dumps(state))
        # Schedule first reveal
        if reveal_urls:
            schedule_post(gid, "blurred_vision",
                          _future_iso(TIMING["blurred_reveal_interval"]),
                          json.dumps({"round_id": rid, "reveal_index": 0}))
        embed = _image_embed(
            "👁️ Blurred Vision",
            f"*What is this?*\n\n"
            f"Fewer reveals used = more points!\n"
            f"• 0 reveals: **{POINTS['blurred_max']} pts**\n"
            f"• Each reveal costs **{POINTS['blurred_decay']} pts**\n\n"
            f"Use `/blurred answer` to identify it!",
            initial_url
        )
        await interaction.response.send_message(embed=embed)

    @blurred_group.command(name="answer", description="Submit your identification.")
    @app_commands.describe(answer="What is the blurred image?")
    @game_check("blurred_vision")
    async def blurred_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "blurred_vision")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Blurred Vision round is live."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already tried", "You've already answered."), ephemeral=True
            )
            return
        if answers_match(answer, state["answer"]):
            reveals = state["reveals_shown"]
            pts = max(POINTS["blurred_max"] - reveals * POINTS["blurred_decay"], 1)
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, "blurred_vision", pts, solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Identified!",
                    f"{interaction.user.mention} identified the image!\n"
                    f"**Answer:** {state['answer']}\n"
                    f"**Reveals used:** {reveals} | **+{pts} pts**"
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Incorrect", "Not quite!"), ephemeral=True
            )

    # ══════════════════════════════════════════════════════════════════════════
    # Generic "post image + first correct ID wins" helper
    # ══════════════════════════════════════════════════════════════════════════

    async def _post_visual_round(self, interaction: discord.Interaction,
                                 game_key: str, title: str, prompt: str,
                                 image_url: str, answer: str, cmd_name: str):
        gid = interaction.guild_id
        state = {
            "answer": answer,
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(gid, game_key, interaction.channel_id, json.dumps(state))
        embed = _image_embed(
            title,
            f"{prompt}\nUse `/{cmd_name} answer` to identify it!",
            image_url
        )
        await interaction.response.send_message(embed=embed)

    async def _visual_answer(self, interaction: discord.Interaction,
                             game_key: str, answer: str, title: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, game_key)
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", f"No {title} round is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already tried", "You've already answered."), ephemeral=True
            )
            return
        if answers_match(answer, state["answer"]):
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, game_key, POINTS["first_correct"], solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    f"✅ Correct!",
                    f"{interaction.user.mention} identified it!\n"
                    f"**Answer:** {state['answer']}\n"
                    f"**+{POINTS['first_correct']} pts**"
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Incorrect", "Not quite!"), ephemeral=True
            )

    # ══════════════════════════════════════════════════════════════════════════
    # MACRO WORLD
    # ══════════════════════════════════════════════════════════════════════════

    macro_group = app_commands.Group(
        name="macro",
        description="Macro World commands.",
        guild_only=True,
    )

    @macro_group.command(name="post", description="[Mod] Post a Macro World close-up image.")
    @app_commands.describe(image_url="URL of the extreme close-up image.", answer="What the object is.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("macro_world")
    async def macro_post(self, interaction: discord.Interaction, image_url: str, answer: str):
        await self._post_visual_round(
            interaction, "macro_world", "🔬 Macro World",
            "What everyday object is this an extreme close-up of?",
            image_url, answer, "macro"
        )

    @macro_group.command(name="answer", description="Identify the macro close-up.")
    @app_commands.describe(answer="What is the object?")
    @game_check("macro_world")
    async def macro_answer(self, interaction: discord.Interaction, answer: str):
        await self._visual_answer(interaction, "macro_world", answer, "Macro World")

    # ══════════════════════════════════════════════════════════════════════════
    # MAP SURGEON
    # ══════════════════════════════════════════════════════════════════════════

    map_group = app_commands.Group(
        name="map",
        description="Map Surgeon commands.",
        guild_only=True,
    )

    @map_group.command(name="post", description="[Mod] Post a Map Surgeon cropped map.")
    @app_commands.describe(image_url="URL of the unlabelled cropped map.", answer="The country/city/region.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("map_surgeon")
    async def map_post(self, interaction: discord.Interaction, image_url: str, answer: str):
        await self._post_visual_round(
            interaction, "map_surgeon", "🗺️ Map Surgeon",
            "What country, city, or region is shown in this unlabelled map section?",
            image_url, answer, "map"
        )

    @map_group.command(name="answer", description="Identify the location on the map.")
    @app_commands.describe(answer="The country, city, or region.")
    @game_check("map_surgeon")
    async def map_answer(self, interaction: discord.Interaction, answer: str):
        await self._visual_answer(interaction, "map_surgeon", answer, "Map Surgeon")

    # ══════════════════════════════════════════════════════════════════════════
    # SILHOUETTE SHOWDOWN
    # ══════════════════════════════════════════════════════════════════════════

    sil_group = app_commands.Group(
        name="silhouette",
        description="Silhouette Showdown commands.",
        guild_only=True,
    )

    @sil_group.command(name="post", description="[Mod] Post a Silhouette Showdown image.")
    @app_commands.describe(
        image_url="URL of the silhouette image.",
        answer="The animal, landmark, or object.",
        difficulty="Difficulty level of this silhouette (1=easy, 5=hard).",
    )
    @app_commands.default_permissions(manage_messages=True)
    @game_check("silhouette_showdown")
    async def sil_post(self, interaction: discord.Interaction,
                       image_url: str, answer: str, difficulty: int = 3):
        gid = interaction.guild_id
        state = {
            "answer": answer,
            "difficulty": difficulty,
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(gid, "silhouette_showdown", interaction.channel_id, json.dumps(state))
        pts = POINTS["first_correct"] + difficulty * 2
        embed = _image_embed(
            "🔲 Silhouette Showdown",
            f"What is this silhouette of?\n"
            f"*Difficulty: {'⭐' * difficulty}* | **{pts} pts** for first correct\n"
            f"Use `/silhouette answer`",
            image_url
        )
        await interaction.response.send_message(embed=embed)

    @sil_group.command(name="answer", description="Identify the silhouette.")
    @app_commands.describe(answer="What is the silhouette?")
    @game_check("silhouette_showdown")
    async def sil_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "silhouette_showdown")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Silhouette Showdown is live."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already tried", "You've already answered."), ephemeral=True
            )
            return
        if answers_match(answer, state["answer"]):
            ms = int((time.time() - state["started_ts"]) * 1000)
            pts = POINTS["first_correct"] + state.get("difficulty", 3) * 2
            award(gid, uid, "silhouette_showdown", pts, solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Silhouette identified!",
                    f"{interaction.user.mention} named it correctly!\n"
                    f"**Answer:** {state['answer']}\n"
                    f"**+{pts} pts**"
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Incorrect", "Not quite!"), ephemeral=True
            )

    # ══════════════════════════════════════════════════════════════════════════
    # COLOUR CODED
    # ══════════════════════════════════════════════════════════════════════════

    colour_group = app_commands.Group(
        name="colour",
        description="Colour Coded commands.",
        guild_only=True,
    )

    @colour_group.command(name="post", description="[Mod] Post a Colour Coded palette.")
    @app_commands.describe(
        image_url="URL of an image showing only the hex palette.",
        answer="The logo, flag, or artwork this palette belongs to.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @game_check("colour_coded")
    async def colour_post(self, interaction: discord.Interaction, image_url: str, answer: str):
        await self._post_visual_round(
            interaction, "colour_coded", "🎨 Colour Coded",
            "What logo, flag, or artwork does this colour palette belong to?",
            image_url, answer, "colour"
        )

    @colour_group.command(name="answer", description="Identify the source of the colour palette.")
    @app_commands.describe(answer="The logo, flag, or artwork.")
    @game_check("colour_coded")
    async def colour_answer(self, interaction: discord.Interaction, answer: str):
        await self._visual_answer(interaction, "colour_coded", answer, "Colour Coded")

    # ══════════════════════════════════════════════════════════════════════════
    # LOGO BLITZ
    # ══════════════════════════════════════════════════════════════════════════

    logo_group = app_commands.Group(
        name="logo",
        description="Logo Blitz commands.",
        guild_only=True,
    )

    @logo_group.command(name="post", description="[Mod] Post a Logo Blitz cropped/modified logo.")
    @app_commands.describe(image_url="URL of the logo image.", answer="The brand name.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("logo_blitz")
    async def logo_post(self, interaction: discord.Interaction, image_url: str, answer: str):
        await self._post_visual_round(
            interaction, "logo_blitz", "🏷️ Logo Blitz",
            "Which brand does this modified/cropped logo belong to?",
            image_url, answer, "logo"
        )

    @logo_group.command(name="answer", description="Identify the brand.")
    @app_commands.describe(answer="The brand name.")
    @game_check("logo_blitz")
    async def logo_answer(self, interaction: discord.Interaction, answer: str):
        await self._visual_answer(interaction, "logo_blitz", answer, "Logo Blitz")

    # ══════════════════════════════════════════════════════════════════════════
    # OPENING LINE
    # ══════════════════════════════════════════════════════════════════════════

    ol_group = app_commands.Group(
        name="ol",
        description="Opening Line commands.",
        guild_only=True,
    )

    @ol_group.command(name="post", description="Post the first line of a book, film, game, or song.")
    @app_commands.describe(
        opening="The opening line.",
        answer="The title (and medium if needed: 'Moby Dick [novel]').",
        submitted_by="Leave blank if posting as mod; fill if this is a member submission."
    )
    @app_commands.default_permissions(manage_messages=True)
    @game_check("opening_line")
    async def ol_post(self, interaction: discord.Interaction,
                      opening: str, answer: str, submitted_by: str = ""):
        gid = interaction.guild_id
        state = {
            "opening": opening,
            "answer": answer,
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(gid, "opening_line", interaction.channel_id, json.dumps(state))
        footer = f"Submitted by {submitted_by}" if submitted_by else ""
        embed = discord.Embed(
            title="📖 Opening Line",
            description=f'*"{opening}"*\n\n'
                        f"What book, film, game, or song does this open?\n"
                        f"Use `/ol answer <title>`",
            colour=discord.Colour.from_str("#5865F2")
        )
        if footer:
            embed.set_footer(text=footer)
        await interaction.response.send_message(embed=embed)

    @ol_group.command(name="submit", description="Submit an opening line for the queue.")
    @app_commands.describe(opening="The opening line.", answer="The source title.")
    @game_check("opening_line")
    async def ol_submit(self, interaction: discord.Interaction, opening: str, answer: str):
        with get_db() as conn:
            conn.execute("""
                INSERT INTO question_pool
                  (guild_id, game_key, author_id, question, answer, approved)
                VALUES (?, 'opening_line', ?, ?, ?, 0)
            """, (interaction.guild_id, interaction.user.id, opening, answer))
        await interaction.response.send_message(
            embed=win_embed("Submitted for review!", "A mod will approve your opening line."),
            ephemeral=True
        )

    @ol_group.command(name="answer", description="Identify the source of the opening line.")
    @app_commands.describe(title="The book, film, game, or song title.")
    @game_check("opening_line")
    async def ol_answer(self, interaction: discord.Interaction, title: str):
        await self._visual_answer(interaction, "opening_line", title, "Opening Line")

    # ══════════════════════════════════════════════════════════════════════════
    # CHARACTER SILHOUETTE
    # ══════════════════════════════════════════════════════════════════════════

    char_group = app_commands.Group(
        name="char",
        description="Character Silhouette commands.",
        guild_only=True,
    )

    @char_group.command(name="post",
                        description="[Mod] Post a fictional character silhouette.")
    @app_commands.describe(
        image_url="URL of the character silhouette.",
        answer="Character name (and source, e.g. 'Pikachu [Pokémon]').",
    )
    @app_commands.default_permissions(manage_messages=True)
    @game_check("character_silhouette")
    async def char_post(self, interaction: discord.Interaction, image_url: str, answer: str):
        await self._post_visual_round(
            interaction, "character_silhouette", "🦸 Character Silhouette",
            "Who is this? Identify the character AND the source medium.\n"
            "Use `/char answer <name [source]>`",
            image_url, answer, "char"
        )

    @char_group.command(name="submit",
                        description="Suggest a character to add to the pool.")
    @app_commands.describe(character="Character name and source (e.g. 'Gollum [Lord of the Rings]')")
    @game_check("character_silhouette")
    async def char_submit(self, interaction: discord.Interaction, character: str):
        with get_db() as conn:
            conn.execute("""
                INSERT INTO question_pool
                  (guild_id, game_key, author_id, question, answer, approved)
                VALUES (?, 'character_silhouette', ?, ?, ?, 0)
            """, (interaction.guild_id, interaction.user.id, character, character))
        await interaction.response.send_message(
            embed=win_embed("Suggestion submitted!", "A mod will add this to the pool."),
            ephemeral=True
        )

    @char_group.command(name="answer", description="Identify the character and source.")
    @app_commands.describe(answer="Character name and source.")
    @game_check("character_silhouette")
    async def char_answer(self, interaction: discord.Interaction, answer: str):
        await self._visual_answer(interaction, "character_silhouette", answer, "Character Silhouette")


async def setup(bot: commands.Bot):
    await bot.add_cog(VisualCog(bot))
