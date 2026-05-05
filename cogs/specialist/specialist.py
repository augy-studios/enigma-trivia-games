"""
cogs/specialist/specialist.py — Specialist knowledge games.

  niche_gauntlet   — Member-submitted questions, no category label. Community identifies field + answer.
  deep_cut         — Obscure corners of well-known topics.
  true_or_truffle  — True/false vote with a speed bonus for fastest correct voter.
  source_check     — Identify who said a quote (free text, checked against API).
"""

import discord
import json
import time
import logging
import asyncio
from datetime import datetime, timezone, timedelta

from discord import app_commands
from discord.ext import commands

from config import POINTS, TIMING
from database import (
    get_db, get_active_round, create_active_round, close_active_round,
    add_points, is_game_enabled,
)
from utils.api import fetch_trivia, fetch_quote
from utils.scoring import (
    game_check, award, answers_match, info_embed, win_embed, warn_embed,
    COLOUR_WIN, COLOUR_INFO,
)

log = logging.getLogger(__name__)


def _state(row) -> dict:
    return json.loads(row["state"])


def _save_state(round_id: int, state: dict):
    with get_db() as conn:
        conn.execute("UPDATE active_rounds SET state=? WHERE id=?",
                     (json.dumps(state), round_id))


class SpecialistCog(commands.Cog, name="Specialist"):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ══════════════════════════════════════════════════════════════════════════
    # NICHE GAUNTLET
    # ══════════════════════════════════════════════════════════════════════════

    niche_group = app_commands.Group(
        name="niche",
        description="Niche Gauntlet commands.",
        guild_only=True,
    )

    @niche_group.command(name="submit",
                         description="Submit a question in your area of expertise (no category label).")
    @app_commands.describe(question="Your question.", answer="The correct answer.")
    @game_check("niche_gauntlet")
    async def niche_submit(self, interaction: discord.Interaction,
                           question: str, answer: str):
        with get_db() as conn:
            conn.execute("""
                INSERT INTO question_pool
                  (guild_id, game_key, author_id, question, answer, approved)
                VALUES (?, 'niche_gauntlet', ?, ?, ?, 1)
            """, (interaction.guild_id, interaction.user.id, question, answer))
            conn.execute("""
                INSERT INTO contributions (guild_id, user_id, questions_submitted)
                VALUES (?, ?, 1)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET questions_submitted=questions_submitted+1
            """, (interaction.guild_id, interaction.user.id))
        await interaction.response.send_message(
            embed=win_embed("Submitted!", "Your Niche Gauntlet question has been queued."),
            ephemeral=True
        )

    @niche_group.command(name="post", description="[Mod] Post the next Niche Gauntlet question.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("niche_gauntlet")
    async def niche_post(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        with get_db() as conn:
            q = conn.execute("""
                SELECT * FROM question_pool
                WHERE guild_id=? AND game_key='niche_gauntlet' AND approved=1 AND times_used=0
                ORDER BY RANDOM() LIMIT 1
            """, (gid,)).fetchone()
        if not q:
            await interaction.response.send_message(
                embed=warn_embed("Empty queue", "No unused questions in the Niche Gauntlet queue."),
                ephemeral=True
            )
            return
        state = {
            "question": q["question"],
            "correct_answer": q["answer"],
            "author_id": q["author_id"],
            "question_id": q["id"],
            "field_guesses": {},   # user_id → field guess
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(gid, "niche_gauntlet", interaction.channel_id, json.dumps(state))
        embed = info_embed(
            "🔍 Niche Gauntlet",
            f"**{q['question']}**\n\n"
            f"*No category label — figure out the field AND the answer!*\n\n"
            f"• `/niche answer <answer>` — answer the question\n"
            f"• `/niche field <field>` — guess the field of knowledge"
        )
        await interaction.response.send_message(embed=embed)

    @niche_group.command(name="answer", description="Answer the live Niche Gauntlet question.")
    @app_commands.describe(answer="Your answer.")
    @game_check("niche_gauntlet")
    async def niche_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "niche_gauntlet")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active question", "No Niche Gauntlet question is live."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already tried", "You've already answered."), ephemeral=True
            )
            return
        if answers_match(answer, state["correct_answer"]):
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, "niche_gauntlet", POINTS["first_correct"], solve_ms=ms)
            with get_db() as conn:
                conn.execute(
                    "UPDATE question_pool SET times_used=times_used+1 WHERE id=?",
                    (state["question_id"],)
                )
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Correct!",
                    f"{interaction.user.mention} answered correctly!\n"
                    f"**Answer:** {state['correct_answer']}\n"
                    f"**+{POINTS['first_correct']} pts**"
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            with get_db() as conn:
                conn.execute(
                    "UPDATE question_pool SET times_stumped=times_stumped+1 WHERE id=?",
                    (state["question_id"],)
                )
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Incorrect", "Not quite!"), ephemeral=True
            )

    @niche_group.command(name="field", description="Guess the field of knowledge for the current question.")
    @app_commands.describe(field="The field of knowledge (e.g. 'biochemistry', 'medieval history')")
    @game_check("niche_gauntlet")
    async def niche_field(self, interaction: discord.Interaction, field: str):
        round_row = get_active_round(interaction.guild_id, "niche_gauntlet")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active question", "No Niche Gauntlet question is live."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        state.setdefault("field_guesses", {})[str(interaction.user.id)] = field
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=info_embed("Field guess recorded", f"You guessed: **{field}**"),
            ephemeral=True
        )

    # ══════════════════════════════════════════════════════════════════════════
    # DEEP CUT
    # ══════════════════════════════════════════════════════════════════════════

    @app_commands.command(name="deepcut", description="Post a Deep Cut obscure trivia question.")
    @game_check("deep_cut")
    @app_commands.guild_only()
    async def deep_cut(self, interaction: discord.Interaction):
        # Use hard difficulty from Open Trivia to approximate "deep cut"
        questions = await fetch_trivia(amount=1, difficulty="hard")
        if not questions:
            await interaction.response.send_message(
                embed=warn_embed("API error", "Could not fetch a question."), ephemeral=True
            )
            return
        q = questions[0]
        state = {
            "question": q["question"],
            "correct_answer": q["correct_answer"],
            "category": q["category"],
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(interaction.guild_id, "deep_cut",
                            interaction.channel_id, json.dumps(state))
        embed = info_embed(
            "🕳️ Deep Cut",
            f"**{q['question']}**\n\n"
            f"*Topic: {q['category']} — obscure edition*\n\n"
            f"Use `/dcanswer` to submit!"
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="dcanswer", description="Answer the live Deep Cut question.")
    @app_commands.describe(answer="Your answer.")
    @game_check("deep_cut")
    @app_commands.guild_only()
    async def dc_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "deep_cut")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No question", "No Deep Cut question is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already tried", "You've already answered."), ephemeral=True
            )
            return
        if answers_match(answer, state["correct_answer"]):
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, "deep_cut", POINTS["first_correct"],
                  category=state["category"], solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Deep cut uncovered!",
                    f"{interaction.user.mention} knew the deep cut!\n"
                    f"**Answer:** {state['correct_answer']}\n"
                    f"**+{POINTS['first_correct']} pts**"
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Incorrect", "Not quite deep enough!"), ephemeral=True
            )

    # ══════════════════════════════════════════════════════════════════════════
    # TRUE OR TRUFFLE
    # ══════════════════════════════════════════════════════════════════════════

    tot_group = app_commands.Group(
        name="tot",
        description="True or Truffle commands.",
        guild_only=True,
    )

    @tot_group.command(name="post",
                       description="[Mod] Post a True or Truffle statement.")
    @app_commands.describe(
        statement="A statement that sounds real or made-up.",
        is_true="Is this statement actually true?",
    )
    @app_commands.default_permissions(manage_messages=True)
    @game_check("true_or_truffle")
    async def tot_post(self, interaction: discord.Interaction,
                       statement: str, is_true: bool):
        gid = interaction.guild_id
        if get_active_round(gid, "true_or_truffle"):
            await interaction.response.send_message(
                embed=warn_embed("Already active", "A True or Truffle round is live."),
                ephemeral=True
            )
            return
        state = {
            "statement": statement,
            "is_true": is_true,
            "votes": {},   # user_id → "true"/"false"
            "vote_times": {},  # user_id → timestamp
            "started_ts": time.time(),
        }
        create_active_round(gid, "true_or_truffle", interaction.channel_id, json.dumps(state))
        embed = info_embed(
            "🍄 True or Truffle?",
            f"**{statement}**\n\n"
            f"Is this statement TRUE or FALSE?\n"
            f"Use `/tot vote true` or `/tot vote false`!\n\n"
            f"*Correct majority earns collective points. Fastest correct voter earns a bonus.*"
        )
        await interaction.response.send_message(embed=embed)

    @tot_group.command(name="vote", description="Vote on the True or Truffle statement.")
    @app_commands.describe(vote="Your vote.")
    @app_commands.choices(vote=[
        app_commands.Choice(name="True", value="true"),
        app_commands.Choice(name="False", value="false"),
    ])
    @game_check("true_or_truffle")
    async def tot_vote(self, interaction: discord.Interaction, vote: str):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "true_or_truffle")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No True or Truffle is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state["votes"]:
            await interaction.response.send_message(
                embed=warn_embed("Already voted", "You've already cast your vote."), ephemeral=True
            )
            return
        state["votes"][uid] = vote
        state["vote_times"][uid] = time.time()
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=info_embed("Vote recorded", f"You voted: **{vote.upper()}**"), ephemeral=True
        )

    @tot_group.command(name="reveal", description="[Mod] Reveal the True or Truffle answer.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("true_or_truffle")
    async def tot_reveal(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        round_row = get_active_round(gid, "true_or_truffle")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No True or Truffle is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        correct_vote = "true" if state["is_true"] else "false"
        votes = state["votes"]
        vote_times = state["vote_times"]

        correct_voters = [uid for uid, v in votes.items() if v == correct_vote]
        incorrect_voters = [uid for uid, v in votes.items() if v != correct_vote]

        # Collective point if majority correct
        total = len(votes)
        correct_count = len(correct_voters)
        collective_earned = correct_count > total / 2 if total > 0 else False

        # Speed bonus — fastest among correct
        speed_winner = None
        if correct_voters:
            speed_winner = min(correct_voters, key=lambda u: vote_times.get(u, float("inf")))
            award(gid, int(speed_winner), "true_or_truffle", POINTS["vote_correct_bonus"])

        if collective_earned:
            for uid in correct_voters:
                add_points(gid, int(uid), "true_or_truffle", POINTS["first_correct"])

        close_active_round(round_row["id"])

        answer_str = "TRUE ✅" if state["is_true"] else "FALSE ❌"
        lines = [
            f"**Statement:** {state['statement']}",
            f"**Answer:** {answer_str}",
            f"**Votes:** {correct_count}/{total} correct",
        ]
        if collective_earned:
            lines.append(f"✅ Majority correct! All {correct_count} correct voters earn {POINTS['first_correct']} pts.")
        if speed_winner:
            lines.append(f"⚡ Speed bonus: <@{speed_winner}> was fastest! +{POINTS['vote_correct_bonus']} pts")

        await interaction.response.send_message(
            embed=win_embed("🍄 True or Truffle Revealed!", "\n".join(lines))
        )

    # ══════════════════════════════════════════════════════════════════════════
    # SOURCE CHECK
    # ══════════════════════════════════════════════════════════════════════════

    source_group = app_commands.Group(
        name="source",
        description="Source Check commands.",
        guild_only=True,
    )

    @source_group.command(name="post",
                          description="Post a quote — members must identify who said it.")
    @game_check("source_check")
    async def source_post(self, interaction: discord.Interaction):
        quote = await fetch_quote()
        if not quote:
            await interaction.response.send_message(
                embed=warn_embed("API error", "Could not fetch a quote."), ephemeral=True
            )
            return
        gid = interaction.guild_id
        state = {
            "content": quote["content"],
            "author": quote["author"],
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(gid, "source_check", interaction.channel_id, json.dumps(state))
        embed = info_embed(
            "📜 Source Check",
            f'*"{quote["content"]}"*\n\n'
            f"**Who said this?** Use `/source answer <name>` — free text, no multiple choice!"
        )
        await interaction.response.send_message(embed=embed)

    @source_group.command(name="answer", description="Submit who said the quote.")
    @app_commands.describe(name="The name of the person who said the quote.")
    @game_check("source_check")
    async def source_answer(self, interaction: discord.Interaction, name: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "source_check")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active quote", "No Source Check is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already tried", "You've already answered."), ephemeral=True
            )
            return
        if answers_match(name, state["author"]):
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, "source_check", POINTS["first_correct"], solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Correct attribution!",
                    f"{interaction.user.mention} identified the source!\n"
                    f"**Author:** {state['author']}\n"
                    f"**+{POINTS['first_correct']} pts**"
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Wrong attribution", "That's not the source."), ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(SpecialistCog(bot))
