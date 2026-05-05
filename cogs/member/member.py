"""
cogs/member/member.py
Member-Generated Content games:
  - question_curator  : Members submit & upvote questions into the active pool
  - fact_drop         : Daily member-submitted facts, community voting
  - the_examiner      : Rotating examiner posts 5 questions, earns stump points
"""

from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import (
    get_db,
    get_game_config,
    is_game_enabled,
    get_active_round,
    create_active_round,
    close_active_round,
    add_points,
    add_category_points,
    schedule_post,
    get_pending_schedules,
    mark_schedule_fired,
    record_solve_time,
    update_streak,
)
from utils.scoring import (
    game_check,
    award,
    make_embed,
    win_embed,
    info_embed,
    warn_embed,
    COLOUR_WARN as GOLD, COLOUR_WIN as GREEN, COLOUR_LOSS as RED, COLOUR_INFO as BLUE,
)
from config import POINTS, TIMING


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _state(row) -> dict:
    return json.loads(row["state"]) if row and row["state"] else {}


def _save_state(round_id: int, state: dict):
    db = get_db()
    db.execute(
        "UPDATE active_rounds SET state=? WHERE id=?",
        (json.dumps(state), round_id),
    )
    db.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# ─────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────

class MemberCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scheduler_loop.start()

    def cog_unload(self):
        self.scheduler_loop.cancel()

    # ──────────────────────────────────────────
    # Scheduler
    # ──────────────────────────────────────────

    @tasks.loop(seconds=60)
    async def scheduler_loop(self):
        now = datetime.now(timezone.utc).isoformat()
        rows = get_pending_schedules(now)
        for row in rows:
            try:
                await self._fire_schedule(row)
            except Exception as e:
                print(f"[MemberCog scheduler] error: {e}")
            mark_schedule_fired(row["id"])

    @scheduler_loop.before_loop
    async def before_scheduler(self):
        await self.bot.wait_until_ready()

    async def _fire_schedule(self, row):
        payload = json.loads(row["payload"])
        game_key = row["game_key"]
        guild = self.bot.get_guild(row["guild_id"])
        if not guild:
            return

        if game_key == "fact_drop":
            await self._post_daily_fact(guild, payload)

    # ──────────────────────────────────────────────────────────────────────────
    # QUESTION CURATOR
    # ──────────────────────────────────────────────────────────────────────────
    # Members submit questions; community upvotes; mods approve into active pool.
    # /qc submit   — submit a question + answer
    # /qc upvote   — upvote a pending question by its ID
    # /qc approve  — mod: move a question into the active pool
    # /qc pool     — list approved questions (truncated)
    # /qc stats    — leaderboard: whose questions get used/upvoted most
    # ──────────────────────────────────────────────────────────────────────────

    qc = app_commands.Group(name="qc", description="Question Curator — build the question bank together")

    @qc.command(name="submit", description="Submit a question to the community pool")
    @app_commands.describe(question="Your question", answer="The correct answer")
    @game_check("question_curator")
    async def qc_submit(self, interaction: discord.Interaction, question: str, answer: str):
        db = get_db()
        db.execute(
            """INSERT INTO question_pool
               (guild_id, submitted_by, question, answer, status, upvotes, times_used, submitted_at)
               VALUES (?,?,?,?,'pending',0,0,?)""",
            (interaction.guild_id, interaction.user.id, question, answer, _now_iso()),
        )
        db.commit()
        qid = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        embed = make_embed(
            "📬 Question Submitted",
            f"**Q:** {question}\n**ID:** `{qid}`\n\nA mod can approve it with `/qc approve {qid}`.\n"
            f"Community can upvote with `/qc upvote {qid}`.",
            BLUE,
        )
        await interaction.response.send_message(embed=embed)

    @qc.command(name="upvote", description="Upvote a pending question")
    @app_commands.describe(question_id="The question ID to upvote")
    @game_check("question_curator")
    async def qc_upvote(self, interaction: discord.Interaction, question_id: int):
        db = get_db()
        row = db.execute(
            "SELECT * FROM question_pool WHERE id=? AND guild_id=?",
            (question_id, interaction.guild_id),
        ).fetchone()
        if not row:
            await interaction.response.send_message(embed=warn_embed("Question not found."), ephemeral=True)
            return
        db.execute(
            "UPDATE question_pool SET upvotes=upvotes+1 WHERE id=?", (question_id,)
        )
        db.commit()
        new_votes = row["upvotes"] + 1
        await interaction.response.send_message(
            embed=info_embed("👍 Upvoted", f"Question `{question_id}` now has **{new_votes}** upvote(s)."),
            ephemeral=True,
        )

    @qc.command(name="approve", description="[Mod] Approve a question into the active pool")
    @app_commands.describe(question_id="The question ID to approve")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("question_curator")
    async def qc_approve(self, interaction: discord.Interaction, question_id: int):
        db = get_db()
        row = db.execute(
            "SELECT * FROM question_pool WHERE id=? AND guild_id=?",
            (question_id, interaction.guild_id),
        ).fetchone()
        if not row:
            await interaction.response.send_message(embed=warn_embed("Question not found."), ephemeral=True)
            return
        db.execute(
            "UPDATE question_pool SET status='approved' WHERE id=?", (question_id,)
        )
        db.commit()
        submitter = await self.bot.fetch_user(row["submitted_by"])
        embed = make_embed(
            "✅ Question Approved",
            f"**Q:** {row['question']}\nSubmitted by **{submitter.display_name}** — now in the active pool.",
            GREEN,
        )
        await interaction.response.send_message(embed=embed)

    @qc.command(name="pool", description="Browse approved questions in the pool")
    @game_check("question_curator")
    async def qc_pool(self, interaction: discord.Interaction):
        db = get_db()
        rows = db.execute(
            "SELECT id, question, upvotes, times_used FROM question_pool "
            "WHERE guild_id=? AND status='approved' ORDER BY upvotes DESC LIMIT 10",
            (interaction.guild_id,),
        ).fetchall()
        if not rows:
            await interaction.response.send_message(embed=info_embed("Pool Empty", "No approved questions yet."))
            return
        lines = [f"`{r['id']}` ⬆️{r['upvotes']} 🔁{r['times_used']}x — {r['question'][:60]}" for r in rows]
        embed = make_embed("📚 Question Pool (Top 10)", "\n".join(lines), BLUE)
        await interaction.response.send_message(embed=embed)

    @qc.command(name="stats", description="See whose questions are used and upvoted most")
    @game_check("question_curator")
    async def qc_stats(self, interaction: discord.Interaction):
        db = get_db()
        top_used = db.execute(
            "SELECT submitted_by, SUM(times_used) as total FROM question_pool "
            "WHERE guild_id=? AND status='approved' GROUP BY submitted_by ORDER BY total DESC LIMIT 5",
            (interaction.guild_id,),
        ).fetchall()
        top_voted = db.execute(
            "SELECT submitted_by, SUM(upvotes) as total FROM question_pool "
            "WHERE guild_id=? GROUP BY submitted_by ORDER BY total DESC LIMIT 5",
            (interaction.guild_id,),
        ).fetchall()

        async def fmt(rows, key):
            lines = []
            for i, r in enumerate(rows, 1):
                u = await self.bot.fetch_user(r["submitted_by"])
                lines.append(f"{i}. **{u.display_name}** — {r[key]}")
            return "\n".join(lines) or "No data yet."

        embed = make_embed("📊 Question Curator Stats", "", BLUE)
        embed.add_field(name="🔁 Most Used Questions", value=await fmt(top_used, "total"), inline=False)
        embed.add_field(name="⬆️ Most Upvoted Questions", value=await fmt(top_voted, "total"), inline=False)
        await interaction.response.send_message(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────
    # FACT DROP
    # ──────────────────────────────────────────────────────────────────────────
    # Members submit facts with sources. Bot posts one per day. Community votes.
    # /fd submit   — submit a fact + source
    # /fd schedule — mod: schedule daily fact posts at HH:MM UTC
    # /fd vote     — upvote the current day's fact
    # /fd stats    — leaderboard: whose facts score highest on average
    # ──────────────────────────────────────────────────────────────────────────

    fd = app_commands.Group(name="fd", description="Fact Drop — share interesting facts, community votes")

    @fd.command(name="submit", description="Submit an interesting fact with a source")
    @app_commands.describe(fact="The fact", source="Where you found it (URL, book, etc.)")
    @game_check("fact_drop")
    async def fd_submit(self, interaction: discord.Interaction, fact: str, source: str):
        db = get_db()
        db.execute(
            """INSERT INTO submissions
               (guild_id, game_key, user_id, content, extra, submitted_at, status)
               VALUES (?,?,?,?,?,'pending',?)""",
            # re-using submissions table: content=fact, extra=source
            (interaction.guild_id, "fact_drop", interaction.user.id, fact, source, _now_iso()),
        )
        db.commit()
        fid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        embed = make_embed(
            "📨 Fact Submitted",
            f"**Fact:** {fact}\n**Source:** {source}\n\nID: `{fid}` — a mod can schedule it with `/fd schedule`.",
            BLUE,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @fd.command(name="schedule", description="[Mod] Schedule a fact to be posted at a specific UTC time today")
    @app_commands.describe(fact_id="The submission ID", time_utc="HH:MM in UTC (e.g. 09:00)")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("fact_drop")
    async def fd_schedule(self, interaction: discord.Interaction, fact_id: int, time_utc: str):
        db = get_db()
        row = db.execute(
            "SELECT * FROM submissions WHERE id=? AND guild_id=? AND game_key='fact_drop'",
            (fact_id, interaction.guild_id),
        ).fetchone()
        if not row:
            await interaction.response.send_message(embed=warn_embed("Fact not found."), ephemeral=True)
            return

        try:
            h, m = map(int, time_utc.split(":"))
        except ValueError:
            await interaction.response.send_message(embed=warn_embed("Use HH:MM format."), ephemeral=True)
            return

        now = datetime.now(timezone.utc)
        fire_at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if fire_at <= now:
            fire_at += timedelta(days=1)

        cfg = get_game_config(interaction.guild_id, "fact_drop")
        channel_id = cfg["channel_id"] if cfg else interaction.channel_id

        schedule_post(
            interaction.guild_id,
            "fact_drop",
            fire_at.isoformat(),
            json.dumps({
                "fact_id": fact_id,
                "fact": row["content"],
                "source": row["extra"],
                "author_id": row["user_id"],
                "channel_id": channel_id,
            }),
        )
        db.execute("UPDATE submissions SET status='scheduled' WHERE id=?", (fact_id,))
        db.commit()

        embed = info_embed(
            "⏰ Fact Scheduled",
            f"Fact `{fact_id}` will be posted at **{fire_at.strftime('%H:%M UTC')}**.",
        )
        await interaction.response.send_message(embed=embed)

    @fd.command(name="vote", description="Upvote the current day's fact")
    @game_check("fact_drop")
    async def fd_vote(self, interaction: discord.Interaction):
        round_row = get_active_round(interaction.guild_id, "fact_drop")
        if not round_row:
            await interaction.response.send_message(embed=warn_embed("No active fact right now."), ephemeral=True)
            return
        state = _state(round_row)
        voter_id = str(interaction.user.id)
        if voter_id in state.get("voters", []):
            await interaction.response.send_message(embed=warn_embed("You've already voted on this fact."), ephemeral=True)
            return
        state.setdefault("votes", 0)
        state.setdefault("voters", [])
        state["votes"] += 1
        state["voters"].append(voter_id)
        _save_state(round_row["id"], state)

        # Award submitter a small point for each vote received
        add_points(interaction.guild_id, state["author_id"], "fact_drop", 1)

        embed = info_embed("👍 Voted!", f"This fact now has **{state['votes']}** vote(s).")
        await interaction.response.send_message(embed=embed)

    @fd.command(name="stats", description="See whose facts score highest on average")
    @game_check("fact_drop")
    async def fd_stats(self, interaction: discord.Interaction):
        db = get_db()
        # We track votes as points in the points table with game_key='fact_drop'
        rows = db.execute(
            "SELECT user_id, points FROM points WHERE guild_id=? AND game_key='fact_drop' "
            "ORDER BY points DESC LIMIT 10",
            (interaction.guild_id,),
        ).fetchall()
        if not rows:
            await interaction.response.send_message(embed=info_embed("No data yet.", "Submit some facts first!"))
            return
        lines = []
        for i, r in enumerate(rows, 1):
            u = await self.bot.fetch_user(r["user_id"])
            lines.append(f"{i}. **{u.display_name}** — {r['points']} vote(s) earned")
        embed = make_embed("📊 Fact Drop Leaderboard", "\n".join(lines), BLUE)
        await interaction.response.send_message(embed=embed)

    async def _post_daily_fact(self, guild: discord.Guild, payload: dict):
        channel = guild.get_channel(payload["channel_id"])
        if not channel:
            return

        # Close previous round
        prev = get_active_round(guild.id, "fact_drop")
        if prev:
            close_active_round(prev["id"])

        author = await self.bot.fetch_user(payload["author_id"])
        embed = make_embed(
            "💡 Daily Fact Drop",
            f"{payload['fact']}\n\n📎 *Source: {payload['source']}*\n\nSubmitted by **{author.display_name}**\n"
            f"Use `/fd vote` to rate this fact!",
            GOLD,
        )
        msg = await channel.send(embed=embed)

        round_id = create_active_round(
            guild.id, "fact_drop", channel.id, msg.id,
            json.dumps({
                "fact_id": payload["fact_id"],
                "author_id": payload["author_id"],
                "votes": 0,
                "voters": [],
            }),
        )

    # ──────────────────────────────────────────────────────────────────────────
    # THE EXAMINER
    # ──────────────────────────────────────────────────────────────────────────
    # Rotating examiner role. Examiner posts 5 questions; earns points for stumps.
    # /exam appoint  — mod: appoint someone as examiner
    # /exam post     — examiner: post the next question in their set
    # /exam answer   — members: answer the active examiner question
    # /exam status   — see current examiner and question progress
    # /exam board    — leaderboard of best examiners
    # ──────────────────────────────────────────────────────────────────────────

    exam = app_commands.Group(name="exam", description="The Examiner — rotating question master")

    @exam.command(name="appoint", description="[Mod] Appoint a member as the current examiner")
    @app_commands.describe(member="The member to appoint as examiner")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("the_examiner")
    async def exam_appoint(self, interaction: discord.Interaction, member: discord.Member):
        round_row = get_active_round(interaction.guild_id, "the_examiner")
        if round_row:
            state = _state(round_row)
            if state.get("phase") == "active":
                await interaction.response.send_message(
                    embed=warn_embed("An examiner round is already in progress. Wait for it to finish."),
                    ephemeral=True,
                )
                return
            close_active_round(round_row["id"])

        round_id = create_active_round(
            interaction.guild_id, "the_examiner", interaction.channel_id, 0,
            json.dumps({
                "examiner_id": member.id,
                "phase": "waiting",          # waiting → active → done
                "questions": [],             # list of {q, a, solvers:[]}
                "current_q": -1,             # index of active question (-1 = none yet)
                "total_stumps": 0,
            }),
        )
        embed = make_embed(
            "🎓 Examiner Appointed",
            f"**{member.display_name}** is now the Examiner!\n\n"
            f"They should use `/exam post` to begin posting their 5 questions.\n"
            f"Each question they stump the server on earns them points!",
            GOLD,
        )
        await interaction.response.send_message(embed=embed)
        await interaction.channel.send(
            f"📣 {member.mention} — you're the Examiner! Post your first question with `/exam post`."
        )

    @exam.command(name="post", description="[Examiner only] Post your next question")
    @app_commands.describe(question="Your question", answer="The correct answer (kept secret)")
    @game_check("the_examiner")
    async def exam_post(self, interaction: discord.Interaction, question: str, answer: str):
        round_row = get_active_round(interaction.guild_id, "the_examiner")
        if not round_row:
            await interaction.response.send_message(embed=warn_embed("No examiner session active."), ephemeral=True)
            return
        state = _state(round_row)
        if state["examiner_id"] != interaction.user.id:
            await interaction.response.send_message(embed=warn_embed("You are not the current Examiner."), ephemeral=True)
            return
        if len(state["questions"]) >= 5:
            await interaction.response.send_message(embed=warn_embed("You've already posted all 5 questions."), ephemeral=True)
            return
        if state.get("current_q", -1) >= 0 and state["phase"] == "active":
            # Check if current question still open
            q = state["questions"][state["current_q"]]
            if not q.get("closed"):
                await interaction.response.send_message(
                    embed=warn_embed("Close the current question first — wait for it to be answered or time out."),
                    ephemeral=True,
                )
                return

        q_index = len(state["questions"])
        state["questions"].append({
            "q": question,
            "a": answer.lower().strip(),
            "solvers": [],
            "closed": False,
        })
        state["current_q"] = q_index
        state["phase"] = "active"
        _save_state(round_row["id"], state)

        embed = make_embed(
            f"🎓 Examiner Q{q_index + 1}/5",
            f"**{question}**\n\nAnswer with `/exam answer`.",
            GOLD,
        )
        msg = await interaction.channel.send(embed=embed)
        await interaction.response.send_message(
            embed=info_embed("Question Posted", f"Q{q_index + 1} is live!"), ephemeral=True
        )

        # Schedule auto-close after 48h
        schedule_post(
            interaction.guild_id,
            "the_examiner_close",
            _iso_after(TIMING.get("the_examiner", 172800)),
            json.dumps({
                "round_id": round_row["id"],
                "q_index": q_index,
                "channel_id": interaction.channel_id,
                "message_id": msg.id,
            }),
        )

    @exam.command(name="answer", description="Answer the current examiner question")
    @app_commands.describe(answer="Your answer")
    @game_check("the_examiner")
    async def exam_answer(self, interaction: discord.Interaction, answer: str):
        round_row = get_active_round(interaction.guild_id, "the_examiner")
        if not round_row:
            await interaction.response.send_message(embed=warn_embed("No active question right now."), ephemeral=True)
            return
        state = _state(round_row)
        if state.get("current_q", -1) < 0 or state["phase"] != "active":
            await interaction.response.send_message(embed=warn_embed("No question is currently live."), ephemeral=True)
            return
        q = state["questions"][state["current_q"]]
        if q.get("closed"):
            await interaction.response.send_message(embed=warn_embed("That question has already closed."), ephemeral=True)
            return
        if interaction.user.id == state["examiner_id"]:
            await interaction.response.send_message(embed=warn_embed("The Examiner can't answer their own question."), ephemeral=True)
            return
        if interaction.user.id in q["solvers"]:
            await interaction.response.send_message(embed=warn_embed("You've already answered correctly."), ephemeral=True)
            return

        from utils.scoring import answers_match
        if not answers_match(answer, q["a"]):
            await interaction.response.send_message(embed=warn_embed("❌ Wrong! Keep trying."), ephemeral=True)
            return

        # Correct!
        q["solvers"].append(interaction.user.id)
        # First solver gets full points; subsequent get fewer
        pts = POINTS.get("the_examiner_solver", 3)
        award(interaction.guild_id, interaction.user.id, "the_examiner", pts)

        # If 3+ people solved it, close the question (not a stump)
        is_first = len(q["solvers"]) == 1
        _save_state(round_row["id"], state)

        embed = win_embed(
            "✅ Correct!" if is_first else "✅ Also Correct!",
            f"**{interaction.user.display_name}** got it! +{pts} pts",
        )
        await interaction.response.send_message(embed=embed)

    @exam.command(name="close", description="[Mod/Examiner] Close the current question and reveal the answer")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("the_examiner")
    async def exam_close(self, interaction: discord.Interaction):
        round_row = get_active_round(interaction.guild_id, "the_examiner")
        if not round_row:
            await interaction.response.send_message(embed=warn_embed("No active examiner session."), ephemeral=True)
            return
        state = _state(round_row)
        qi = state.get("current_q", -1)
        if qi < 0 or state["questions"][qi].get("closed"):
            await interaction.response.send_message(embed=warn_embed("No open question to close."), ephemeral=True)
            return

        q = state["questions"][qi]
        q["closed"] = True
        stumped = len(q["solvers"]) == 0
        state["total_stumps"] += 1 if stumped else 0

        # If nobody solved it, examiner earns stump points
        if stumped:
            stump_pts = POINTS.get("the_examiner_stump", 5)
            award(interaction.guild_id, state["examiner_id"], "the_examiner", stump_pts)

        # If all 5 questions done, wrap up
        all_done = len(state["questions"]) >= 5 and all(q2.get("closed") for q2 in state["questions"])
        if all_done:
            state["phase"] = "done"
            close_active_round(round_row["id"])
        else:
            _save_state(round_row["id"], state)

        examiner = await self.bot.fetch_user(state["examiner_id"])
        solver_text = (
            f"Nobody solved it — **{examiner.display_name}** earns stump points! 🎉"
            if stumped
            else f"**{len(q['solvers'])}** member(s) got it right."
        )
        embed = make_embed(
            f"📖 Q{qi + 1} Closed — Answer: {q['a'].title()}",
            solver_text + (
                f"\n\n✅ Examiner session complete! {state['total_stumps']}/5 stumped." if all_done else ""
            ),
            GREEN if not stumped else GOLD,
        )
        await interaction.response.send_message(embed=embed)

    @exam.command(name="status", description="See the current examiner session status")
    @game_check("the_examiner")
    async def exam_status(self, interaction: discord.Interaction):
        round_row = get_active_round(interaction.guild_id, "the_examiner")
        if not round_row:
            await interaction.response.send_message(embed=info_embed("No Active Session", "No examiner is currently active."))
            return
        state = _state(round_row)
        examiner = await self.bot.fetch_user(state["examiner_id"])
        q_done = sum(1 for q in state["questions"] if q.get("closed"))
        qi = state.get("current_q", -1)
        current = f"**Q{qi + 1}:** {state['questions'][qi]['q']}" if qi >= 0 and not state["questions"][qi].get("closed") else "Waiting for next question."
        embed = make_embed(
            "🎓 Examiner Status",
            f"**Examiner:** {examiner.display_name}\n"
            f"**Progress:** {q_done}/5 questions done\n"
            f"**Stumps so far:** {state['total_stumps']}\n\n"
            f"**Current:** {current}",
            BLUE,
        )
        await interaction.response.send_message(embed=embed)

    @exam.command(name="board", description="See the all-time examiner leaderboard")
    @game_check("the_examiner")
    async def exam_board(self, interaction: discord.Interaction):
        db = get_db()
        rows = db.execute(
            "SELECT user_id, points FROM points WHERE guild_id=? AND game_key='the_examiner' "
            "ORDER BY points DESC LIMIT 10",
            (interaction.guild_id,),
        ).fetchall()
        if not rows:
            await interaction.response.send_message(embed=info_embed("No data yet.", "Appoint an examiner to get started."))
            return
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(rows, 1):
            u = await self.bot.fetch_user(r["user_id"])
            medal = medals[i - 1] if i <= 3 else f"{i}."
            lines.append(f"{medal} **{u.display_name}** — {r['points']} pts")
        embed = make_embed("🎓 Examiner Leaderboard", "\n".join(lines), GOLD)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MemberCog(bot))
