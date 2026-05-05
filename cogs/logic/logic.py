"""
cogs/logic/logic.py — Logic, Reasoning, Strategy & Estimation games.

  logic_lock        — 48 h window; first correct max points, diminishing returns.
  pattern_breaker   — Identify the anomaly AND explain why.
  missing_link      — Find the word connecting three unrelated words.
  contradiction     — Find the logical contradiction in a paragraph.
  inference_engine  — Deduce the answer from clues; more clues = fewer points.
  fermi_estimator   — Closest estimation wins.
  price_is_right    — Closest without going over.
  probability_pulse — Estimate a probability %; closest to verified stat wins.
  timeline_toss     — Guess the year of a historical event; closest wins.
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
    get_db, get_active_round, create_active_round, close_active_round, add_points,
)
from utils.scoring import (
    game_check, award, answers_match, info_embed, win_embed, warn_embed, normalise,
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


class LogicCog(commands.Cog, name="Logic"):
    """Logic, Reasoning, and Strategy games for Enigma."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.expiry_loop.start()

    def cog_unload(self):
        self.expiry_loop.cancel()

    @tasks.loop(seconds=120)
    async def expiry_loop(self):
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            expired = conn.execute("""
                SELECT * FROM active_rounds
                WHERE game_key IN
                  ('logic_lock','fermi_estimator','price_is_right',
                   'probability_pulse','timeline_toss','inference_engine')
                AND expires_at IS NOT NULL AND expires_at <= ?
            """, (now,)).fetchall()
        for row in expired:
            try:
                await self._resolve_expired(row)
                close_active_round(row["id"])
            except Exception as e:
                log.error("expiry_loop error %s: %s", row["id"], e)

    @expiry_loop.before_loop
    async def before_expiry(self):
        await self.bot.wait_until_ready()

    async def _resolve_expired(self, row):
        gid = row["guild_id"]
        key = row["game_key"]
        state = _state(row)
        channel = self.bot.get_channel(row["channel_id"])
        if not channel:
            return
        if key in ("fermi_estimator", "price_is_right", "probability_pulse", "timeline_toss"):
            await self._resolve_estimation(gid, key, state, channel)

    async def _resolve_estimation(self, gid, key, state, channel):
        guesses = state.get("guesses", {})
        correct = state.get("correct_value")
        if correct is None or not guesses:
            await channel.send(embed=warn_embed("Round ended", "No guesses were submitted."))
            return

        correct = float(correct)
        if key == "price_is_right":
            # Closest without going over
            valid = {uid: float(g) for uid, g in guesses.items() if float(g) <= correct}
            if valid:
                winner = min(valid, key=lambda u: correct - valid[u])
            else:
                winner = None
        else:
            winner = min(guesses, key=lambda u: abs(float(guesses[u]) - correct))

        label = {
            "fermi_estimator": f"Correct answer: {correct:,.0f}",
            "price_is_right": f"Actual price: {correct:,.2f}",
            "probability_pulse": f"Verified probability: {correct:.1f}%",
            "timeline_toss": f"Correct year: {int(correct)}",
        }.get(key, f"Answer: {correct}")

        lines = [f"**{label}**\n"]
        if winner:
            pts = POINTS.get(key, POINTS["fermi_winner"])
            add_points(gid, int(winner), key, pts)
            lines.append(
                f"🏆 <@{winner}> wins with guess **{guesses[winner]}**! +{pts} pts"
            )
        else:
            lines.append("Nobody guessed correctly (Price Is Right: all over the target).")

        await channel.send(embed=win_embed("🎯 Results!", "\n".join(lines)))

    # ══════════════════════════════════════════════════════════════════════════
    # LOGIC LOCK
    # ══════════════════════════════════════════════════════════════════════════

    ll_group = app_commands.Group(
        name="ll",
        description="Logic Lock commands.",
        guild_only=True,
    )

    @ll_group.command(name="post", description="[Mod] Post a Logic Lock puzzle.")
    @app_commands.describe(puzzle="The logic puzzle.", answer="The correct answer.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("logic_lock")
    async def ll_post(self, interaction: discord.Interaction, puzzle: str, answer: str):
        gid = interaction.guild_id
        expires = _future_iso(TIMING["logic_lock_window"])
        state = {
            "puzzle": puzzle,
            "answer": answer,
            "correct_count": 0,
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(gid, "logic_lock", interaction.channel_id,
                            json.dumps(state), expires)
        embed = info_embed(
            "🔒 Logic Lock",
            f"**{puzzle}**\n\n"
            f"Submit within **48 hours** using `/ll answer`.\n"
            f"First correct: **{POINTS['logic_lock_max']} pts** | Each subsequent: "
            f"**{max(POINTS['logic_lock_max'] - POINTS['logic_lock_decay'], 1)} pts** (diminishing)"
        )
        await interaction.response.send_message(embed=embed)

    @ll_group.command(name="answer", description="Submit your Logic Lock answer.")
    @app_commands.describe(answer="Your answer.")
    @game_check("logic_lock")
    async def ll_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "logic_lock")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active puzzle", "No Logic Lock is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already answered", "You've already solved this one."),
                ephemeral=True
            )
            return
        if answers_match(answer, state["answer"]):
            n = state["correct_count"]
            pts = max(POINTS["logic_lock_max"] - n * POINTS["logic_lock_decay"], 1)
            state["correct_count"] += 1
            state["answered_by"].append(uid)
            _save_state(round_row["id"], state)
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, "logic_lock", pts, solve_ms=ms)
            await interaction.response.send_message(
                embed=win_embed(
                    f"✅ Correct! (Solver #{n+1})",
                    f"{interaction.user.mention} cracked the Logic Lock!\n"
                    f"**+{pts} pts** | Total solvers: {n+1}"
                )
            )
        else:
            await interaction.response.send_message(
                embed=warn_embed("❌ Incorrect", "Not the right answer."), ephemeral=True
            )

    # ══════════════════════════════════════════════════════════════════════════
    # PATTERN BREAKER
    # ══════════════════════════════════════════════════════════════════════════

    pb_group = app_commands.Group(
        name="pb",
        description="Pattern Breaker commands.",
        guild_only=True,
    )

    @pb_group.command(name="post", description="[Mod] Post a Pattern Breaker sequence.")
    @app_commands.describe(
        sequence="Comma-separated items (e.g. '2, 4, 7, 8, 16')",
        anomaly="The item that breaks the pattern.",
        reason="Why it's the anomaly.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @game_check("pattern_breaker")
    async def pb_post(self, interaction: discord.Interaction,
                      sequence: str, anomaly: str, reason: str):
        gid = interaction.guild_id
        state = {
            "sequence": sequence,
            "anomaly": anomaly,
            "reason": reason,
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(gid, "pattern_breaker", interaction.channel_id, json.dumps(state))
        embed = info_embed(
            "🔍 Pattern Breaker",
            f"**Sequence:** `{sequence}`\n\n"
            f"Find the item that **doesn't belong** and explain why.\n"
            f"Use `/pb answer <item> | <reason>` (both must be correct!)"
        )
        await interaction.response.send_message(embed=embed)

    @pb_group.command(name="answer", description="Submit the anomaly and your reasoning.")
    @app_commands.describe(
        item="The anomaly item.",
        reason="Why it breaks the pattern.",
    )
    @game_check("pattern_breaker")
    async def pb_answer(self, interaction: discord.Interaction, item: str, reason: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "pattern_breaker")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active puzzle", "No Pattern Breaker is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already answered", "You've already answered."), ephemeral=True
            )
            return
        item_ok = answers_match(item, state["anomaly"])
        reason_ok = answers_match(reason, state["reason"])
        if item_ok and reason_ok:
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, "pattern_breaker", POINTS["first_correct"], solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Pattern broken correctly!",
                    f"{interaction.user.mention} identified the anomaly!\n"
                    f"**Anomaly:** {state['anomaly']}\n"
                    f"**Reason:** {state['reason']}\n"
                    f"**+{POINTS['first_correct']} pts**"
                )
            )
        elif item_ok:
            await interaction.response.send_message(
                embed=warn_embed("Anomaly correct, reasoning off",
                                 "You found the right item but your explanation needs work!"),
                ephemeral=True
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Incorrect", "Neither the item nor the reason matched."),
                ephemeral=True
            )

    # ══════════════════════════════════════════════════════════════════════════
    # THE MISSING LINK
    # ══════════════════════════════════════════════════════════════════════════

    ml_group = app_commands.Group(
        name="link",
        description="The Missing Link commands.",
        guild_only=True,
    )

    @ml_group.command(name="post", description="[Mod] Post a Missing Link puzzle.")
    @app_commands.describe(
        word_a="First word.", word_b="Second word.", word_c="Third word.",
        link="The word that connects all three."
    )
    @app_commands.default_permissions(manage_messages=True)
    @game_check("missing_link")
    async def ml_post(self, interaction: discord.Interaction,
                      word_a: str, word_b: str, word_c: str, link: str):
        gid = interaction.guild_id
        state = {
            "words": [word_a, word_b, word_c],
            "link": link,
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(gid, "missing_link", interaction.channel_id, json.dumps(state))
        embed = info_embed(
            "🔗 The Missing Link",
            f"**{word_a}** · **{word_b}** · **{word_c}**\n\n"
            f"What single word connects all three?\n"
            f"Use `/link answer <word>`"
        )
        await interaction.response.send_message(embed=embed)

    @ml_group.command(name="answer", description="Submit the missing link word.")
    @app_commands.describe(word="The connecting word.")
    @game_check("missing_link")
    async def ml_answer(self, interaction: discord.Interaction, word: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "missing_link")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active puzzle", "No Missing Link is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already tried", "You've already answered."), ephemeral=True
            )
            return
        if answers_match(word, state["link"]):
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, "missing_link", POINTS["first_correct"], solve_ms=ms)
            close_active_round(round_row["id"])
            words = " · ".join(f"**{w}**" for w in state["words"])
            await interaction.response.send_message(
                embed=win_embed(
                    "🔗 Link found!",
                    f"{interaction.user.mention} found the connection!\n"
                    f"{words} → **{state['link']}**\n"
                    f"**+{POINTS['first_correct']} pts**"
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Not the link", "That's not the connecting word."),
                ephemeral=True
            )

    # ══════════════════════════════════════════════════════════════════════════
    # CONTRADICTION SPOTTER
    # ══════════════════════════════════════════════════════════════════════════

    cs_group = app_commands.Group(
        name="cs",
        description="Contradiction Spotter commands.",
        guild_only=True,
    )

    @cs_group.command(name="post", description="[Mod] Post a paragraph with a hidden contradiction.")
    @app_commands.describe(
        paragraph="Short paragraph with one logical contradiction.",
        contradiction="The exact contradicting statement or phrase.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @game_check("contradiction")
    async def cs_post(self, interaction: discord.Interaction,
                      paragraph: str, contradiction: str):
        gid = interaction.guild_id
        state = {
            "paragraph": paragraph,
            "contradiction": contradiction,
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(gid, "contradiction", interaction.channel_id, json.dumps(state))
        embed = info_embed(
            "🧩 Contradiction Spotter",
            f"{paragraph}\n\n"
            f"*One statement above is logically contradictory. Find it!*\n"
            f"Use `/cs answer <contradiction>`"
        )
        await interaction.response.send_message(embed=embed)

    @cs_group.command(name="answer", description="Identify the contradiction.")
    @app_commands.describe(contradiction="The contradicting part of the text.")
    @game_check("contradiction")
    async def cs_answer(self, interaction: discord.Interaction, contradiction: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "contradiction")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active puzzle", "No Contradiction Spotter is live."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already tried", "You've already answered."), ephemeral=True
            )
            return
        if answers_match(contradiction, state["contradiction"]):
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, "contradiction", POINTS["first_correct"], solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Contradiction spotted!",
                    f"{interaction.user.mention} found the flaw!\n"
                    f"**Contradiction:** {state['contradiction']}\n"
                    f"**+{POINTS['first_correct']} pts**"
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ That's not it", "Look more carefully."), ephemeral=True
            )

    # ══════════════════════════════════════════════════════════════════════════
    # INFERENCE ENGINE
    # ══════════════════════════════════════════════════════════════════════════

    inf_group = app_commands.Group(
        name="inf",
        description="Inference Engine commands.",
        guild_only=True,
    )

    @inf_group.command(name="post", description="[Mod] Post an Inference Engine puzzle.")
    @app_commands.describe(
        answer="The hidden answer (person, place, object, or event).",
        clue1="First clue (most vague, max points).",
        clue2="Second clue.",
        clue3="Third clue.",
        clue4="Fourth clue (optional).",
        clue5="Fifth clue (most specific, min points).",
    )
    @app_commands.default_permissions(manage_messages=True)
    @game_check("inference_engine")
    async def inf_post(self, interaction: discord.Interaction,
                       answer: str, clue1: str, clue2: str, clue3: str,
                       clue4: str = "", clue5: str = ""):
        gid = interaction.guild_id
        clues = [c for c in [clue1, clue2, clue3, clue4, clue5] if c]
        state = {
            "answer": answer,
            "clues": clues,
            "clues_revealed": 1,
            "answered_by": [],
            "started_ts": time.time(),
        }
        rid = create_active_round(gid, "inference_engine", interaction.channel_id,
                                  json.dumps(state))
        pts_now = POINTS["escalating_max"]
        embed = info_embed(
            "🕵️ Inference Engine",
            f"**Clue 1:** {clue1}\n\n"
            f"*Deduce the hidden answer — person, place, object, or event.*\n"
            f"Use `/inf answer`. More clues = fewer points.\n"
            f"Current max: **{pts_now} pts** | Use `/inf next` for another clue."
        )
        await interaction.response.send_message(embed=embed)

    @inf_group.command(name="next", description="Reveal the next clue (costs points).")
    @game_check("inference_engine")
    async def inf_next(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        round_row = get_active_round(gid, "inference_engine")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active puzzle", "No Inference Engine puzzle is live."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        rev = state["clues_revealed"]
        if rev >= len(state["clues"]):
            await interaction.response.send_message(
                embed=warn_embed("No more clues", "All clues have been revealed."), ephemeral=True
            )
            return
        state["clues_revealed"] += 1
        _save_state(round_row["id"], state)
        pts_now = max(POINTS["escalating_max"] - state["clues_revealed"] * POINTS["hint_penalty"], 1)
        await interaction.response.send_message(
            embed=info_embed(
                f"🕵️ Inference Engine — Clue {state['clues_revealed']}",
                f"**Clue:** {state['clues'][state['clues_revealed']-1]}\n\n"
                f"Current max points: **{pts_now}**"
            )
        )

    @inf_group.command(name="answer", description="Submit your inference.")
    @app_commands.describe(answer="Your deduced answer.")
    @game_check("inference_engine")
    async def inf_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "inference_engine")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active puzzle", "No Inference Engine puzzle is live."),
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
            used = state["clues_revealed"]
            pts = max(POINTS["escalating_max"] - used * POINTS["hint_penalty"], 1)
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, "inference_engine", pts, solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Deduction correct!",
                    f"{interaction.user.mention} identified the answer!\n"
                    f"**Answer:** {state['answer']}\n"
                    f"**Clues used:** {used} | **+{pts} pts**"
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Wrong deduction", "Keep reasoning!"), ephemeral=True
            )

    # ══════════════════════════════════════════════════════════════════════════
    # FERMI ESTIMATOR
    # ══════════════════════════════════════════════════════════════════════════

    def _estimation_group(name, desc, game_key):
        """Factory for estimation game slash-command groups."""
        return app_commands.Group(name=name, description=desc, guild_only=True)

    fermi_group = app_commands.Group(
        name="fermi",
        description="Fermi Estimator commands.",
        guild_only=True,
    )

    @fermi_group.command(name="post", description="[Mod] Post a Fermi estimation question.")
    @app_commands.describe(question="The estimation question.", answer="The correct numerical answer.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("fermi_estimator")
    async def fermi_post(self, interaction: discord.Interaction, question: str, answer: float):
        gid = interaction.guild_id
        expires = _future_iso(TIMING["logic_lock_window"])
        state = {
            "question": question,
            "correct_value": answer,
            "guesses": {},
        }
        create_active_round(gid, "fermi_estimator", interaction.channel_id,
                            json.dumps(state), expires)
        embed = info_embed(
            "📐 Fermi Estimator",
            f"**{question}**\n\n"
            f"There's no exact right or wrong answer — closest wins!\n"
            f"Use `/fermi guess <number>` — 48 hours to submit."
        )
        await interaction.response.send_message(embed=embed)

    @fermi_group.command(name="guess", description="Submit your Fermi estimate.")
    @app_commands.describe(estimate="Your numerical estimate.")
    @game_check("fermi_estimator")
    async def fermi_guess(self, interaction: discord.Interaction, estimate: float):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "fermi_estimator")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active question", "No Fermi question is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        state.setdefault("guesses", {})[uid] = estimate
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=info_embed("Estimate recorded!", f"Your guess: **{estimate:,.0f}**"),
            ephemeral=True
        )

    @fermi_group.command(name="reveal", description="[Mod] Reveal the Fermi Estimator answer.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("fermi_estimator")
    async def fermi_reveal(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        round_row = get_active_round(gid, "fermi_estimator")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active question", "No Fermi question is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        close_active_round(round_row["id"])
        await self._resolve_estimation(gid, "fermi_estimator", state, interaction.channel)
        await interaction.response.send_message(
            embed=info_embed("Round closed", "Results posted!"), ephemeral=True
        )

    # ══════════════════════════════════════════════════════════════════════════
    # PRICE IS RIGHT
    # ══════════════════════════════════════════════════════════════════════════

    price_group = app_commands.Group(
        name="price",
        description="Price Is Right commands.",
        guild_only=True,
    )

    @price_group.command(name="post", description="[Mod] Post a Price Is Right item.")
    @app_commands.describe(item="The product or statistic.", price="The actual price/value.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("price_is_right")
    async def price_post(self, interaction: discord.Interaction, item: str, price: float):
        gid = interaction.guild_id
        expires = _future_iso(TIMING["logic_lock_window"])
        state = {
            "item": item,
            "correct_value": price,
            "guesses": {},
        }
        create_active_round(gid, "price_is_right", interaction.channel_id,
                            json.dumps(state), expires)
        embed = info_embed(
            "💰 Price Is Right",
            f"**Item:** {item}\n\n"
            f"Guess the price! **Closest without going over wins.**\n"
            f"Use `/price guess <amount>`"
        )
        await interaction.response.send_message(embed=embed)

    @price_group.command(name="guess", description="Submit your price guess.")
    @app_commands.describe(amount="Your price guess.")
    @game_check("price_is_right")
    async def price_guess(self, interaction: discord.Interaction, amount: float):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "price_is_right")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active item", "No Price Is Right item is posted."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        state.setdefault("guesses", {})[uid] = amount
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=info_embed("Guess recorded!", f"Your guess: **{amount:,.2f}**"), ephemeral=True
        )

    @price_group.command(name="reveal", description="[Mod] Reveal the actual price.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("price_is_right")
    async def price_reveal(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        round_row = get_active_round(gid, "price_is_right")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active item", "No Price Is Right item is posted."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        close_active_round(round_row["id"])
        await self._resolve_estimation(gid, "price_is_right", state, interaction.channel)
        await interaction.response.send_message(
            embed=info_embed("Round closed", "Results posted!"), ephemeral=True
        )

    # ══════════════════════════════════════════════════════════════════════════
    # PROBABILITY PULSE
    # ══════════════════════════════════════════════════════════════════════════

    prob_group = app_commands.Group(
        name="prob",
        description="Probability Pulse commands.",
        guild_only=True,
    )

    @prob_group.command(name="post", description="[Mod] Post a Probability Pulse scenario.")
    @app_commands.describe(scenario="The real-world scenario.", probability="The verified % probability.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("probability_pulse")
    async def prob_post(self, interaction: discord.Interaction, scenario: str, probability: float):
        gid = interaction.guild_id
        expires = _future_iso(TIMING["logic_lock_window"])
        state = {
            "scenario": scenario,
            "correct_value": probability,
            "guesses": {},
        }
        create_active_round(gid, "probability_pulse", interaction.channel_id,
                            json.dumps(state), expires)
        embed = info_embed(
            "🎲 Probability Pulse",
            f"**Scenario:** {scenario}\n\n"
            f"What is the probability this happens? Submit as a percentage.\n"
            f"Use `/prob guess <percentage>` — closest to the verified stat wins!"
        )
        await interaction.response.send_message(embed=embed)

    @prob_group.command(name="guess", description="Submit your probability estimate.")
    @app_commands.describe(percentage="Your estimate as a % (e.g. 37.5)")
    @game_check("probability_pulse")
    async def prob_guess(self, interaction: discord.Interaction, percentage: float):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "probability_pulse")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active scenario", "No Probability Pulse is live."),
                ephemeral=True
            )
            return
        if not 0 <= percentage <= 100:
            await interaction.response.send_message(
                embed=warn_embed("Invalid input", "Percentage must be between 0 and 100."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        state.setdefault("guesses", {})[uid] = percentage
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=info_embed("Guess recorded!", f"Your estimate: **{percentage}%**"), ephemeral=True
        )

    @prob_group.command(name="reveal", description="[Mod] Reveal the correct probability.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("probability_pulse")
    async def prob_reveal(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        round_row = get_active_round(gid, "probability_pulse")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active scenario", "No Probability Pulse is live."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        close_active_round(round_row["id"])
        await self._resolve_estimation(gid, "probability_pulse", state, interaction.channel)
        await interaction.response.send_message(
            embed=info_embed("Round closed", "Results posted!"), ephemeral=True
        )

    # ══════════════════════════════════════════════════════════════════════════
    # TIMELINE TOSS
    # ══════════════════════════════════════════════════════════════════════════

    tl_group = app_commands.Group(
        name="tl",
        description="Timeline Toss commands.",
        guild_only=True,
    )

    @tl_group.command(name="post", description="[Mod] Post a Timeline Toss event.")
    @app_commands.describe(event="Historical event (no date given).", year="The correct year.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("timeline_toss")
    async def tl_post(self, interaction: discord.Interaction, event: str, year: int):
        gid = interaction.guild_id
        expires = _future_iso(TIMING["logic_lock_window"])
        state = {
            "event": event,
            "correct_value": float(year),
            "guesses": {},
        }
        create_active_round(gid, "timeline_toss", interaction.channel_id,
                            json.dumps(state), expires)
        embed = info_embed(
            "📅 Timeline Toss",
            f"**Event:** {event}\n\n"
            f"What year did this happen? Closest guess wins!\n"
            f"Use `/tl guess <year>` — points scale by how close you are."
        )
        await interaction.response.send_message(embed=embed)

    @tl_group.command(name="guess", description="Submit your year guess.")
    @app_commands.describe(year="Your year guess.")
    @game_check("timeline_toss")
    async def tl_guess(self, interaction: discord.Interaction, year: int):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "timeline_toss")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active event", "No Timeline Toss is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        state.setdefault("guesses", {})[uid] = float(year)
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=info_embed("Guess recorded!", f"Your year: **{year}**"), ephemeral=True
        )

    @tl_group.command(name="reveal", description="[Mod] Reveal the correct year and award points.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("timeline_toss")
    async def tl_reveal(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        round_row = get_active_round(gid, "timeline_toss")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active event", "No Timeline Toss is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        close_active_round(round_row["id"])
        # Custom resolution: award points scaled by closeness
        correct = float(state["correct_value"])
        guesses = state.get("guesses", {})
        if not guesses:
            await interaction.channel.send(
                embed=warn_embed("No guesses", "Nobody guessed!")
            )
            await interaction.response.send_message(
                embed=info_embed("Round closed", "No guesses submitted."), ephemeral=True
            )
            return

        distances = {uid: abs(float(g) - correct) for uid, g in guesses.items()}
        sorted_guesses = sorted(distances.items(), key=lambda x: x[1])
        max_dist = max(d for _, d in distances.items()) or 1

        lines = [f"**Correct year: {int(correct)}**\n"]
        for i, (uid, dist) in enumerate(sorted_guesses[:5]):
            pts = max(int(POINTS["timeline_winner"] * (1 - dist / max_dist)), 1)
            add_points(gid, int(uid), "timeline_toss", pts)
            medal = ["🥇", "🥈", "🥉", "4.", "5."][i]
            lines.append(
                f"{medal} <@{uid}> — guessed **{int(float(guesses[uid]))}** "
                f"(off by {int(dist)}y) → +{pts} pts"
            )

        await interaction.channel.send(embed=win_embed("📅 Timeline Toss — Results!", "\n".join(lines)))
        await interaction.response.send_message(
            embed=info_embed("Round closed", "Results posted!"), ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(LogicCog(bot))
