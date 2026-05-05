"""
cogs/trivia/classic.py — Classic Trivia games.

Games:
  classic_trivia      — Open Bounty: always-live trivia, first correct answer wins.
  stump_server        — Member-submitted questions posted every 24 h.
  category_king       — Category-tagged trivia with per-category leaderboards.
  escalating_enigma   — Riddle with hints every 6 h; fewer hints = more points.
  chain_reaction      — Correct answer unlocks next question; wrong answer resets chain.
  collective_iq       — Cooperative: all components must be submitted collectively.
"""

import discord
import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta

from discord import app_commands
from discord.ext import commands, tasks

from config import POINTS, TIMING, GAME_REGISTRY
from database import (
    get_db, get_active_round, create_active_round, close_active_round,
    add_points, record_solve_time, schedule_post, get_pending_schedules,
    mark_schedule_fired, is_game_enabled, get_game_config,
)
from utils.api import fetch_trivia, fetch_trivia_categories
from utils.scoring import (
    game_check, award, answers_match, info_embed, win_embed, warn_embed,
    COLOUR_WIN, COLOUR_INFO, format_leaderboard,
)

log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _state(round_row) -> dict:
    return json.loads(round_row["state"])


def _save_state(round_id: int, state: dict):
    with get_db() as conn:
        conn.execute("UPDATE active_rounds SET state=? WHERE id=?",
                     (json.dumps(state), round_id))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _get_guild_channel(bot: commands.Bot, guild_id: int, game_key: str,
                       fallback_channel_id: int | None = None):
    config = get_game_config(guild_id, game_key)
    ch_id = (config["channel_id"] if config and config["channel_id"]
              else fallback_channel_id)
    return bot.get_channel(ch_id) if ch_id else None


# ── Cog ───────────────────────────────────────────────────────────────────────

class ClassicTriviaCog(commands.Cog, name="ClassicTrivia"):
    """Classic trivia games for Enigma."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._classic_locks: dict[int, asyncio.Lock] = {}
        self.scheduler_loop.start()

    def cog_unload(self):
        self.scheduler_loop.cancel()

    # ── Scheduler ────────────────────────────────────────────────────────────

    @tasks.loop(seconds=60)
    async def scheduler_loop(self):
        now = _now_iso()
        pending = get_pending_schedules(now)
        for sched in pending:
            try:
                await self._fire_schedule(sched)
            except Exception as e:
                log.error("Scheduler error for %s: %s", sched["id"], e)
            mark_schedule_fired(sched["id"])

    @scheduler_loop.before_loop
    async def before_scheduler(self):
        await self.bot.wait_until_ready()

    async def _fire_schedule(self, sched):
        gid = sched["guild_id"]
        key = sched["game_key"]
        payload = json.loads(sched["payload"] or "{}")
        if key == "stump_server":
            await self._post_stump_question(gid, payload)
        elif key == "escalating_enigma":
            await self._drop_escalating_hint(gid, payload)

    # ══════════════════════════════════════════════════════════════════════════
    # CLASSIC TRIVIA — Open Bounty
    # ══════════════════════════════════════════════════════════════════════════

    trivia_group = app_commands.Group(
        name="trivia",
        description="Classic trivia commands.",
        guild_only=True,
    )

    @trivia_group.command(name="start", description="Post a new open-bounty trivia question.")
    @game_check("classic_trivia")
    async def trivia_start(self, interaction: discord.Interaction):
        await self._post_classic_question(interaction.guild_id, interaction.channel)
        await interaction.response.send_message(
            embed=info_embed("Trivia", "A new question has been posted!"), ephemeral=True
        )

    @trivia_group.command(name="answer", description="Submit your answer to the live trivia question.")
    @app_commands.describe(answer="Your answer.")
    @game_check("classic_trivia")
    async def trivia_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = interaction.user.id

        lock = self._classic_locks.setdefault(gid, asyncio.Lock())
        async with lock:
            round_row = get_active_round(gid, "classic_trivia")
            if not round_row:
                await interaction.response.send_message(
                    embed=warn_embed("No active question", "Use `/trivia start` to post one."),
                    ephemeral=True
                )
                return

            state = _state(round_row)
            if uid in state.get("answered_by", []):
                await interaction.response.send_message(
                    embed=warn_embed("Already answered", "You already answered this question."),
                    ephemeral=True
                )
                return

            correct = answers_match(answer, state["correct_answer"])
            if correct:
                ms = int((time.time() - state["started_ts"]) * 1000)
                pts = POINTS["first_correct"] if not state.get("answered_by") else POINTS["subsequent_correct"]
                award(gid, uid, "classic_trivia", pts,
                      category=state.get("category"), solve_ms=ms)
                close_active_round(round_row["id"])
                await interaction.response.send_message(
                    embed=win_embed(
                        "✅ Correct!",
                        f"{interaction.user.mention} answered correctly!\n"
                        f"**Answer:** {state['correct_answer']}\n"
                        f"**+{pts} points**"
                    )
                )
                # Post next question automatically
                asyncio.create_task(self._post_classic_question(gid, interaction.channel))
            else:
                state.setdefault("answered_by", []).append(uid)
                _save_state(round_row["id"], state)
                await interaction.response.send_message(
                    embed=warn_embed("❌ Incorrect", "That's not right — keep trying!"),
                    ephemeral=True
                )

    async def _post_classic_question(self, guild_id: int, channel):
        questions = await fetch_trivia(amount=1)
        if not questions:
            return
        q = questions[0]
        state = {
            "question": q["question"],
            "correct_answer": q["correct_answer"],
            "category": q["category"],
            "difficulty": q["difficulty"],
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(guild_id, "classic_trivia", channel.id, json.dumps(state))
        diff_emoji = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}.get(q["difficulty"], "⚪")
        embed = info_embed(
            f"{diff_emoji} Open Bounty Trivia",
            f"**{q['question']}**\n\n"
            f"*Category: {q['category']} | Difficulty: {q['difficulty']}*\n\n"
            f"Use `/trivia answer` to submit your answer!"
        )
        await channel.send(embed=embed)

    # ══════════════════════════════════════════════════════════════════════════
    # STUMP THE SERVER
    # ══════════════════════════════════════════════════════════════════════════

    stump_group = app_commands.Group(
        name="stump",
        description="Stump the Server commands.",
        guild_only=True,
    )

    @stump_group.command(name="submit", description="Submit a question to stump the server.")
    @app_commands.describe(
        question="Your question.",
        answer="The sealed correct answer (only you and the bot know)."
    )
    @game_check("stump_server")
    async def stump_submit(self, interaction: discord.Interaction,
                           question: str, answer: str):
        with get_db() as conn:
            conn.execute("""
                INSERT INTO question_pool
                  (guild_id, game_key, author_id, question, answer, approved)
                VALUES (?, 'stump_server', ?, ?, ?, 1)
            """, (interaction.guild_id, interaction.user.id, question, answer))
            conn.execute("""
                INSERT INTO contributions (guild_id, user_id, questions_submitted)
                VALUES (?, ?, 1)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET questions_submitted=questions_submitted+1
            """, (interaction.guild_id, interaction.user.id))
        await interaction.response.send_message(
            embed=win_embed("Question submitted!",
                            "Your question has been added to the Stump the Server queue."),
            ephemeral=True
        )

    @stump_group.command(name="answer", description="Answer the live Stump the Server question.")
    @app_commands.describe(answer="Your answer.")
    @game_check("stump_server")
    async def stump_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = interaction.user.id

        round_row = get_active_round(gid, "stump_server")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active question", "No Stump the Server question is live right now."),
                ephemeral=True
            )
            return

        state = _state(round_row)
        if uid == state.get("author_id"):
            await interaction.response.send_message(
                embed=warn_embed("Nice try", "You can't answer your own question!"),
                ephemeral=True
            )
            return
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already tried", "You've already submitted an answer."),
                ephemeral=True
            )
            return

        correct = answers_match(answer, state["correct_answer"])
        if correct:
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, "stump_server", POINTS["solver_win"], solve_ms=ms)
            with get_db() as conn:
                conn.execute(
                    "UPDATE question_pool SET times_used=times_used+1 WHERE id=?",
                    (state["question_id"],)
                )
            close_active_round(round_row["id"])
            ch = self.bot.get_channel(round_row["channel_id"])
            if ch:
                await ch.send(embed=win_embed(
                    "✅ Solved!",
                    f"{interaction.user.mention} cracked the stump!\n"
                    f"**Answer:** {state['correct_answer']}\n"
                    f"**Question by:** <@{state['author_id']}>\n"
                    f"**+{POINTS['solver_win']} points** for the solver."
                ))
            await interaction.response.send_message(
                embed=win_embed("Correct!", f"+{POINTS['solver_win']} points!"),
                ephemeral=True
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("Incorrect", "Not quite — keep thinking!"),
                ephemeral=True
            )

    async def _post_stump_question(self, guild_id: int, payload: dict):
        with get_db() as conn:
            q = conn.execute("""
                SELECT * FROM question_pool
                WHERE guild_id=? AND game_key='stump_server' AND approved=1 AND times_used=0
                ORDER BY RANDOM() LIMIT 1
            """, (guild_id,)).fetchone()
        if not q:
            return

        config = get_game_config(guild_id, "stump_server")
        if not config or not config["channel_id"]:
            return
        channel = self.bot.get_channel(config["channel_id"])
        if not channel:
            return

        expires = _future_iso(TIMING["stump_solver_window"])
        state = {
            "question": q["question"],
            "correct_answer": q["answer"],
            "author_id": q["author_id"],
            "question_id": q["id"],
            "answered_by": [],
            "started_ts": time.time(),
            "expires_at": expires,
        }
        rid = create_active_round(guild_id, "stump_server", channel.id,
                                  json.dumps(state), expires)
        embed = info_embed(
            "🔒 Stump the Server",
            f"**{q['question']}**\n\n"
            f"*Submitted by a member — answer sealed.*\n"
            f"Use `/stump answer` to submit! First correct answer wins.\n"
            f"If nobody solves it in 48 hours, the stumper earns a point instead."
        )
        msg = await channel.send(embed=embed)
        # Schedule stumper award after 48 h
        schedule_post(guild_id, "stump_server",
                      expires, json.dumps({"round_id": rid, "question_id": q["id"],
                                           "author_id": q["author_id"]}))
        # Schedule next question
        schedule_post(guild_id, "stump_server",
                      _future_iso(TIMING["stump_post_interval"]), "{}")

    # ══════════════════════════════════════════════════════════════════════════
    # CATEGORY KING
    # ══════════════════════════════════════════════════════════════════════════

    @app_commands.command(name="categoryking", description="Post a category-tagged trivia question.")
    @game_check("category_king")
    @app_commands.guild_only()
    async def category_king(self, interaction: discord.Interaction):
        questions = await fetch_trivia(amount=1)
        if not questions:
            await interaction.response.send_message(
                embed=warn_embed("API error", "Could not fetch a trivia question. Try again."),
                ephemeral=True
            )
            return
        q = questions[0]
        state = {
            "question": q["question"],
            "correct_answer": q["correct_answer"],
            "category": q["category"],
            "difficulty": q["difficulty"],
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(interaction.guild_id, "category_king",
                            interaction.channel_id, json.dumps(state))
        diff_emoji = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}.get(q["difficulty"], "⚪")
        embed = info_embed(
            f"👑 Category King — {q['category']}",
            f"**{q['question']}**\n\n"
            f"{diff_emoji} *Difficulty: {q['difficulty']}*\n\n"
            f"Use `/ck answer` to submit. Earn **category-specific** points!"
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ckanswer", description="Answer the live Category King question.")
    @app_commands.describe(answer="Your answer.")
    @game_check("category_king")
    @app_commands.guild_only()
    async def ck_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "category_king")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Category King question is live."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already answered", "You've already answered this question."),
                ephemeral=True
            )
            return
        if answers_match(answer, state["correct_answer"]):
            ms = int((time.time() - state["started_ts"]) * 1000)
            pts = POINTS["category_king"]
            award(gid, uid, "category_king", pts, category=state["category"], solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Correct!",
                    f"{interaction.user.mention} wins the **{state['category']}** crown!\n"
                    f"**Answer:** {state['correct_answer']}\n"
                    f"**+{pts} points** added to {state['category']} leaderboard."
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Incorrect", "Not quite!"), ephemeral=True
            )

    # ══════════════════════════════════════════════════════════════════════════
    # ESCALATING ENIGMA
    # ══════════════════════════════════════════════════════════════════════════

    escalating_group = app_commands.Group(
        name="enigma",
        description="Escalating Enigma commands.",
        guild_only=True,
    )

    @escalating_group.command(name="post", description="Post a new Escalating Enigma riddle.")
    @app_commands.describe(
        riddle="The riddle question.",
        answer="The correct answer.",
        hint1="First hint (shown after 6 h).",
        hint2="Second hint (shown after 12 h).",
        hint3="Third hint (shown after 18 h).",
    )
    @game_check("escalating_enigma")
    async def escalating_post(self, interaction: discord.Interaction,
                               riddle: str, answer: str,
                               hint1: str, hint2: str, hint3: str):
        gid = interaction.guild_id
        existing = get_active_round(gid, "escalating_enigma")
        if existing:
            await interaction.response.send_message(
                embed=warn_embed("Already active", "There's already a live Escalating Enigma riddle."),
                ephemeral=True
            )
            return
        state = {
            "riddle": riddle,
            "correct_answer": answer,
            "hints": [hint1, hint2, hint3],
            "hints_shown": 0,
            "answered_by": [],
            "started_ts": time.time(),
        }
        rid = create_active_round(gid, "escalating_enigma", interaction.channel_id,
                                  json.dumps(state))
        # Schedule hint drops
        for i in range(3):
            schedule_post(gid, "escalating_enigma",
                          _future_iso(TIMING["escalating_hint_interval"] * (i + 1)),
                          json.dumps({"round_id": rid, "hint_index": i}))
        embed = info_embed(
            "🔮 Escalating Enigma",
            f"**{riddle}**\n\n"
            f"*No hints yet. Solve with fewer hints for more points!*\n"
            f"• 0 hints used: **{POINTS['escalating_max']} pts**\n"
            f"• Each hint costs **{POINTS['hint_penalty']} pts**\n\n"
            f"Use `/enigma answer` to submit!"
        )
        await interaction.response.send_message(embed=embed)

    @escalating_group.command(name="answer", description="Answer the live Escalating Enigma riddle.")
    @app_commands.describe(answer="Your answer.")
    @game_check("escalating_enigma")
    async def escalating_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "escalating_enigma")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No riddle", "No Escalating Enigma is active."), ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already tried", "You've already answered."), ephemeral=True
            )
            return
        if answers_match(answer, state["correct_answer"]):
            hints_used = state["hints_shown"]
            pts = max(POINTS["escalating_max"] - hints_used * POINTS["hint_penalty"], 1)
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, "escalating_enigma", pts, solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Enigma Solved!",
                    f"{interaction.user.mention} cracked the riddle!\n"
                    f"**Answer:** {state['correct_answer']}\n"
                    f"**Hints used:** {hints_used} | **+{pts} points**"
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Incorrect", "Keep thinking!"), ephemeral=True
            )

    async def _drop_escalating_hint(self, guild_id: int, payload: dict):
        rid = payload.get("round_id")
        hint_index = payload.get("hint_index", 0)
        with get_db() as conn:
            row = conn.execute("SELECT * FROM active_rounds WHERE id=?", (rid,)).fetchone()
        if not row:
            return
        state = json.loads(row["state"])
        hints = state.get("hints", [])
        if hint_index >= len(hints):
            return
        state["hints_shown"] = hint_index + 1
        _save_state(rid, state)
        channel = self.bot.get_channel(row["channel_id"])
        if channel:
            pts_remaining = max(POINTS["escalating_max"] - (hint_index + 1) * POINTS["hint_penalty"], 1)
            await channel.send(embed=info_embed(
                f"💡 Escalating Enigma — Hint {hint_index + 1}",
                f"**Hint:** {hints[hint_index]}\n\n"
                f"*Max points now: **{pts_remaining}***\n"
                f"Use `/enigma answer` to submit!"
            ))

    # ══════════════════════════════════════════════════════════════════════════
    # CHAIN REACTION TRIVIA
    # ══════════════════════════════════════════════════════════════════════════

    chain_group = app_commands.Group(
        name="chain",
        description="Chain Reaction Trivia commands.",
        guild_only=True,
    )

    @chain_group.command(name="start", description="Start a Chain Reaction Trivia round.")
    @game_check("chain_reaction")
    async def chain_start(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        if get_active_round(gid, "chain_reaction"):
            await interaction.response.send_message(
                embed=warn_embed("Already active", "A chain is already in progress."),
                ephemeral=True
            )
            return
        await self._post_chain_question(gid, interaction.channel, chain_length=0)
        await interaction.response.send_message(
            embed=info_embed("Chain started!", "Question 1 posted."), ephemeral=True
        )

    @chain_group.command(name="answer", description="Answer the current chain question.")
    @app_commands.describe(answer="Your answer.")
    @game_check("chain_reaction")
    async def chain_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "chain_reaction")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active chain", "Start one with `/chain start`."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        if answers_match(answer, state["correct_answer"]):
            chain_len = state.get("chain_length", 0) + 1
            pts = POINTS["chain_per_link"]
            award(gid, uid, "chain_reaction", pts)
            with get_db() as conn:
                conn.execute("""
                    INSERT INTO points (guild_id, user_id, game_key, points)
                    VALUES (?, ?, 'chain_longest', ?)
                    ON CONFLICT(guild_id, user_id, game_key)
                    DO UPDATE SET points = MAX(points, excluded.points)
                """, (gid, uid, chain_len))
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    f"✅ Correct! Chain: {chain_len}",
                    f"{interaction.user.mention} kept the chain alive!\n"
                    f"**+{pts} pts** | Unlocking question {chain_len + 1}…"
                )
            )
            asyncio.create_task(
                self._post_chain_question(gid, interaction.channel, chain_len)
            )
        else:
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=warn_embed(
                    "💥 Chain broken!",
                    f"{interaction.user.mention} broke the chain at question "
                    f"{state.get('chain_length', 0) + 1}.\n"
                    f"Use `/chain start` to start a new chain."
                )
            )

    async def _post_chain_question(self, guild_id: int, channel, chain_length: int):
        questions = await fetch_trivia(amount=1)
        if not questions:
            return
        q = questions[0]
        state = {
            "question": q["question"],
            "correct_answer": q["correct_answer"],
            "category": q["category"],
            "chain_length": chain_length,
            "started_ts": time.time(),
        }
        create_active_round(guild_id, "chain_reaction", channel.id, json.dumps(state))
        embed = info_embed(
            f"⛓️ Chain Reaction — Question {chain_length + 1}",
            f"**{q['question']}**\n\n"
            f"*Category: {q['category']}*\n\n"
            f"Use `/chain answer` — a wrong answer breaks the chain!"
        )
        await channel.send(embed=embed)

    # ══════════════════════════════════════════════════════════════════════════
    # COLLECTIVE IQ
    # ══════════════════════════════════════════════════════════════════════════

    ciq_group = app_commands.Group(
        name="ciq",
        description="Collective IQ — cooperative trivia.",
        guild_only=True,
    )

    @ciq_group.command(name="post", description="Post a Collective IQ question with components.")
    @app_commands.describe(
        question="The hard question.",
        components="Comma-separated list of required answer components (e.g. 'Newton,1687,gravity')",
    )
    @game_check("collective_iq")
    async def ciq_post(self, interaction: discord.Interaction,
                       question: str, components: str):
        gid = interaction.guild_id
        if get_active_round(gid, "collective_iq"):
            await interaction.response.send_message(
                embed=warn_embed("Already active", "A Collective IQ question is already live."),
                ephemeral=True
            )
            return
        parts = [p.strip() for p in components.split(",") if p.strip()]
        state = {
            "question": question,
            "components": parts,
            "found": {},       # component → user_id
            "started_ts": time.time(),
        }
        create_active_round(gid, "collective_iq", interaction.channel_id, json.dumps(state))
        embed = info_embed(
            "🧠 Collective IQ",
            f"**{question}**\n\n"
            f"*This answer has **{len(parts)} components**. Pool your knowledge!*\n"
            f"Found: 0 / {len(parts)}\n\n"
            f"Use `/ciq submit` to contribute a piece of the answer."
        )
        await interaction.response.send_message(embed=embed)

    @ciq_group.command(name="submit", description="Submit a component of the Collective IQ answer.")
    @app_commands.describe(piece="Your partial answer component.")
    @game_check("collective_iq")
    async def ciq_submit(self, interaction: discord.Interaction, piece: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "collective_iq")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active question", "No Collective IQ question is live."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        matched = None
        for comp in state["components"]:
            if comp in state["found"]:
                continue
            if answers_match(piece, comp):
                matched = comp
                break
        if not matched:
            await interaction.response.send_message(
                embed=warn_embed("Not a component", "That doesn't match any missing component."),
                ephemeral=True
            )
            return
        state["found"][matched] = uid
        found_count = len(state["found"])
        total = len(state["components"])
        _save_state(round_row["id"], state)

        if found_count >= total:
            # All found — award everyone
            contributors = set(state["found"].values())
            for cuid in contributors:
                award(gid, cuid, "collective_iq", POINTS["collective_iq_share"])
            close_active_round(round_row["id"])
            credits = " ".join(f"<@{u}>" for u in contributors)
            await interaction.response.send_message(
                embed=win_embed(
                    "🎉 Collective IQ Solved!",
                    f"All {total} components found!\n"
                    f"**Contributors:** {credits}\n"
                    f"**+{POINTS['collective_iq_share']} pts** each."
                )
            )
        else:
            await interaction.response.send_message(
                embed=win_embed(
                    f"✅ Component found! ({found_count}/{total})",
                    f"{interaction.user.mention} contributed: **{matched}**\n"
                    f"Still need {total - found_count} more piece(s)."
                )
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(ClassicTriviaCog(bot))
