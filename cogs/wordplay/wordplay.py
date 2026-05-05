"""
cogs/wordplay/wordplay.py — Word & Language games for Enigma.

  anagram_arena    — Unscramble a word; first correct wins.
  definition_duel  — Fake definitions + real one; vote for the real one.
  synonym_sprint   — Race to submit unique synonyms; last valid one wins.
  etymology_race   — Identify a word's language of origin.
  portmanteau      — Blend two concepts into a portmanteau; community votes.
  spelling_bee     — Async elimination spelling round.
  acronym_arch     — Create acronym expansions; community votes.
  forbidden_word   — Answer trivia without using the forbidden word.
  crossword_clue   — Cryptic crossword clue; first correct wins.
"""

import discord
import json
import time
import random
import logging
from datetime import datetime, timezone, timedelta

from discord import app_commands
from discord.ext import commands, tasks

from config import POINTS, TIMING
from database import (
    get_db, get_active_round, create_active_round, close_active_round,
    add_points, schedule_post, get_pending_schedules, mark_schedule_fired,
)
from utils.api import (
    fetch_random_word, fetch_word_definition, fetch_synonyms,
)
from utils.scoring import (
    game_check, award, answers_match, info_embed, win_embed, warn_embed,
    normalise,
)

log = logging.getLogger(__name__)


def _state(row) -> dict:
    return json.loads(row["state"])


def _save_state(round_id: int, state: dict):
    with get_db() as conn:
        conn.execute("UPDATE active_rounds SET state=? WHERE id=?",
                     (json.dumps(state), round_id))


def _scramble(word: str) -> str:
    chars = list(word)
    random.shuffle(chars)
    while "".join(chars) == word:
        random.shuffle(chars)
    return "".join(chars)


ETYMOLOGY_ANSWERS = {
    # A small curated seed; in prod you'd call an etymology API
    "ballet": "French", "piano": "Italian", "kindergarten": "German",
    "algebra": "Arabic", "avatar": "Sanskrit", "chocolate": "Nahuatl",
    "robot": "Czech", "ketchup": "Malay", "safari": "Swahili",
    "sofa": "Arabic", "jungle": "Hindi", "torpedo": "Latin",
}


class WordplayCog(commands.Cog, name="Wordplay"):
    """Word & Language games for Enigma."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.vote_end_loop.start()

    def cog_unload(self):
        self.vote_end_loop.cancel()

    @tasks.loop(seconds=120)
    async def vote_end_loop(self):
        """Automatically close expired voting rounds."""
        from database import get_db
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            expired = conn.execute("""
                SELECT * FROM active_rounds
                WHERE game_key IN ('definition_duel','portmanteau_party','acronym_architect',
                                   'spelling_bee','forbidden_word')
                  AND expires_at IS NOT NULL AND expires_at <= ? AND state != 'closed'
            """, (now,)).fetchall()
        for row in expired:
            try:
                await self._close_voting_round(row)
            except Exception as e:
                log.error("vote_end_loop error round %s: %s", row["id"], e)

    @vote_end_loop.before_loop
    async def before_vote_loop(self):
        await self.bot.wait_until_ready()

    async def _close_voting_round(self, row):
        gid = row["guild_id"]
        key = row["game_key"]
        state = _state(row)
        channel = self.bot.get_channel(row["channel_id"])
        if not channel:
            return
        close_active_round(row["id"])

        if key == "definition_duel":
            await self._resolve_definition_duel(gid, state, channel)
        elif key == "portmanteau_party":
            await self._resolve_portmanteau(gid, state, channel)
        elif key == "acronym_architect":
            await self._resolve_acronym(gid, state, channel)
        elif key == "spelling_bee":
            await self._resolve_spelling_bee(gid, state, channel)

    # ══════════════════════════════════════════════════════════════════════════
    # ANAGRAM ARENA
    # ══════════════════════════════════════════════════════════════════════════

    anagram_group = app_commands.Group(
        name="anagram",
        description="Anagram Arena commands.",
        guild_only=True,
    )

    @anagram_group.command(name="post", description="Post a new anagram to solve.")
    @game_check("anagram_arena")
    async def anagram_post(self, interaction: discord.Interaction):
        word = await fetch_random_word(min_length=5, max_length=10)
        if not word:
            await interaction.response.send_message(
                embed=warn_embed("API error", "Could not fetch a word."), ephemeral=True
            )
            return
        scrambled = _scramble(word)
        state = {
            "word": word,
            "scrambled": scrambled,
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(interaction.guild_id, "anagram_arena",
                            interaction.channel_id, json.dumps(state))
        embed = info_embed(
            "🔀 Anagram Arena",
            f"**Unscramble this:** `{scrambled.upper()}`\n\n"
            f"*{len(word)} letters* — Use `/anagram answer` to submit!"
        )
        await interaction.response.send_message(embed=embed)

    @anagram_group.command(name="answer", description="Submit your anagram solution.")
    @app_commands.describe(word="Your unscrambled word.")
    @game_check("anagram_arena")
    async def anagram_answer(self, interaction: discord.Interaction, word: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "anagram_arena")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active anagram", "Use `/anagram post` to start one."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already tried", "You've already answered."), ephemeral=True
            )
            return
        if normalise(word) == normalise(state["word"]):
            ms = int((time.time() - state["started_ts"]) * 1000)
            pts = max(POINTS["first_correct"] + len(state["word"]) - 5, POINTS["first_correct"])
            award(gid, uid, "anagram_arena", pts, solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Unscrambled!",
                    f"{interaction.user.mention} solved the anagram!\n"
                    f"**Word:** {state['word']}\n"
                    f"**+{pts} pts** | Solved in {ms/1000:.1f}s"
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Wrong", "That's not it!"), ephemeral=True
            )

    # ══════════════════════════════════════════════════════════════════════════
    # DEFINITION DUEL
    # ══════════════════════════════════════════════════════════════════════════

    defduel_group = app_commands.Group(
        name="defduel",
        description="Definition Duel commands.",
        guild_only=True,
    )

    @defduel_group.command(name="start", description="Start a Definition Duel round.")
    @game_check("definition_duel")
    async def defduel_start(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        if get_active_round(gid, "definition_duel"):
            await interaction.response.send_message(
                embed=warn_embed("Already active", "A Definition Duel is already running."),
                ephemeral=True
            )
            return
        word = await fetch_random_word(min_length=6, max_length=14)
        defn_data = await fetch_word_definition(word) if word else None
        if not defn_data:
            await interaction.response.send_message(
                embed=warn_embed("API error", "Could not fetch a word definition."), ephemeral=True
            )
            return
        import datetime as dt
        expires = (dt.datetime.now(dt.timezone.utc)
                   + dt.timedelta(seconds=TIMING["definition_duel_window"])).isoformat()
        state = {
            "word": defn_data["word"],
            "real_definition": defn_data["definition"],
            "part_of_speech": defn_data["part_of_speech"],
            "fakes": {},      # user_id → fake definition
            "votes": {},      # user_id → target user_id ("real" for real defn)
            "phase": "submit",  # submit → vote → closed
        }
        create_active_round(gid, "definition_duel", interaction.channel_id,
                            json.dumps(state), expires)
        embed = info_embed(
            "📖 Definition Duel",
            f"**Word:** `{defn_data['word']}` *({defn_data['part_of_speech']})*\n\n"
            f"Submit a **fake definition** with `/defduel fake <definition>`!\n"
            f"After submissions close, everyone votes on which definition is real.\n\n"
            f"**Earn points for:**\n"
            f"• Fooling others with your fake definition\n"
            f"• Spotting the real definition among the fakes"
        )
        await interaction.response.send_message(embed=embed)

    @defduel_group.command(name="fake", description="Submit a fake definition for the current word.")
    @app_commands.describe(definition="Your convincing fake definition.")
    @game_check("definition_duel")
    async def defduel_fake(self, interaction: discord.Interaction, definition: str):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "definition_duel")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Definition Duel is running."), ephemeral=True
            )
            return
        state = _state(round_row)
        if state["phase"] != "submit":
            await interaction.response.send_message(
                embed=warn_embed("Voting in progress", "Submissions are closed."), ephemeral=True
            )
            return
        state["fakes"][uid] = definition
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=win_embed("Fake submitted!", f"Your definition: *{definition}*"), ephemeral=True
        )

    @defduel_group.command(name="vote", description="Vote on the real definition.")
    @game_check("definition_duel")
    async def defduel_vote(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "definition_duel")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Definition Duel is running."), ephemeral=True
            )
            return
        state = _state(round_row)
        # Transition to vote phase
        state["phase"] = "vote"
        _save_state(round_row["id"], state)

        all_defs = {"real": state["real_definition"]}
        all_defs.update(state["fakes"])
        keys = list(all_defs.keys())
        random.shuffle(keys)

        options = []
        for k in keys:
            label = "Real" if k == "real" else f"Submitted by <@{k}>"
            options.append(f"**{keys.index(k)+1}.** {all_defs[k]}")

        embed = info_embed(
            f"📖 Definition Duel — Vote! (word: `{state['word']}`)",
            "\n\n".join(options) + "\n\n"
            f"Use `/defduel pick <number>` to vote for the real definition!"
        )
        embed.set_footer(text=f"Options order randomised. Keys: {keys}")
        # Store shuffled order
        state["vote_order"] = keys
        _save_state(round_row["id"], state)
        await interaction.response.send_message(embed=embed)

    @defduel_group.command(name="pick", description="Pick which definition is real.")
    @app_commands.describe(number="The number of the definition you think is real.")
    @game_check("definition_duel")
    async def defduel_pick(self, interaction: discord.Interaction, number: int):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "definition_duel")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Definition Duel is running."), ephemeral=True
            )
            return
        state = _state(round_row)
        order = state.get("vote_order", [])
        idx = number - 1
        if idx < 0 or idx >= len(order):
            await interaction.response.send_message(
                embed=warn_embed("Invalid number", f"Pick a number between 1 and {len(order)}."),
                ephemeral=True
            )
            return
        state["votes"][uid] = order[idx]
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=info_embed("Vote recorded!", f"You picked option {number}."), ephemeral=True
        )

    @defduel_group.command(name="reveal", description="[Mod] Reveal the Definition Duel results.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("definition_duel")
    async def defduel_reveal(self, interaction: discord.Interaction):
        round_row = get_active_round(interaction.guild_id, "definition_duel")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Definition Duel is running."), ephemeral=True
            )
            return
        state = _state(round_row)
        await self._resolve_definition_duel(interaction.guild_id, state, interaction.channel)
        close_active_round(round_row["id"])
        await interaction.response.send_message(
            embed=info_embed("Round closed", "Results posted!"), ephemeral=True
        )

    async def _resolve_definition_duel(self, gid, state, channel):
        votes = state.get("votes", {})
        fakes = state.get("fakes", {})
        # Spots real: voters who picked "real"
        real_spotters = [uid for uid, pick in votes.items() if pick == "real"]
        # Fooled by fake: tally per fake author
        fool_counts: dict[str, int] = {}
        for uid, pick in votes.items():
            if pick != "real" and pick in fakes:
                fool_counts[pick] = fool_counts.get(pick, 0) + 1

        lines = [f"**Real definition:** {state['real_definition']}\n"]
        for uid in real_spotters:
            add_points(gid, int(uid), "definition_duel", POINTS["definition_duel_spot"])
        lines.append(
            f"🔍 **Spotted the real one:** "
            + (", ".join(f"<@{u}>" for u in real_spotters) or "Nobody") +
            f" (+{POINTS['definition_duel_spot']} pts each)"
        )
        for fake_uid, count in fool_counts.items():
            pts = count * POINTS["definition_duel_fool"]
            add_points(gid, int(fake_uid), "definition_duel", pts)
            lines.append(f"🎭 <@{fake_uid}> fooled **{count}** people! (+{pts} pts)")
        await channel.send(embed=win_embed("📖 Definition Duel — Results!", "\n".join(lines)))

    # ══════════════════════════════════════════════════════════════════════════
    # SYNONYM SPRINT
    # ══════════════════════════════════════════════════════════════════════════

    syn_group = app_commands.Group(
        name="synonym",
        description="Synonym Sprint commands.",
        guild_only=True,
    )

    @syn_group.command(name="start", description="Start a Synonym Sprint round.")
    @app_commands.describe(word="The word to find synonyms for.")
    @game_check("synonym_sprint")
    async def synonym_start(self, interaction: discord.Interaction, word: str):
        gid = interaction.guild_id
        if get_active_round(gid, "synonym_sprint"):
            await interaction.response.send_message(
                embed=warn_embed("Already active", "A Synonym Sprint is running."), ephemeral=True
            )
            return
        valid = await fetch_synonyms(word, max_results=50)
        if not valid:
            await interaction.response.send_message(
                embed=warn_embed("No synonyms found", f"Could not find synonyms for '{word}'."),
                ephemeral=True
            )
            return
        state = {
            "word": word,
            "valid_synonyms": valid,
            "used": [],       # synonyms already submitted
            "last_user": None,
            "last_word": None,
        }
        create_active_round(gid, "synonym_sprint", interaction.channel_id, json.dumps(state))
        embed = info_embed(
            "🏃 Synonym Sprint",
            f"**Word:** `{word}`\n\n"
            f"Submit synonyms one at a time with `/synonym submit <word>`!\n"
            f"*Last valid synonym before a repeat or failure wins.*"
        )
        await interaction.response.send_message(embed=embed)

    @syn_group.command(name="submit", description="Submit a synonym.")
    @app_commands.describe(synonym="A synonym not yet used in this round.")
    @game_check("synonym_sprint")
    async def synonym_submit(self, interaction: discord.Interaction, synonym: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "synonym_sprint")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active sprint", "Use `/synonym start` first."), ephemeral=True
            )
            return
        state = _state(round_row)
        s = normalise(synonym)
        if s in [normalise(u) for u in state["used"]]:
            # Repeat — previous person wins
            winner = state.get("last_user")
            if winner:
                award(gid, int(winner), "synonym_sprint", POINTS["first_correct"])
            close_active_round(round_row["id"])
            win_msg = f"<@{winner}> wins the sprint!" if winner else "Nobody wins (no valid synonyms submitted)."
            await interaction.response.send_message(
                embed=win_embed("🏁 Sprint over!", f"{interaction.user.mention} repeated a synonym.\n{win_msg}")
            )
            return
        if s not in [normalise(v) for v in state["valid_synonyms"]]:
            # Invalid synonym — previous person wins
            winner = state.get("last_user")
            if winner:
                award(gid, int(winner), "synonym_sprint", POINTS["first_correct"])
            close_active_round(round_row["id"])
            win_msg = f"<@{winner}> wins!" if winner else "Nobody wins."
            await interaction.response.send_message(
                embed=win_embed("🏁 Sprint over!", f"`{synonym}` isn't a valid synonym.\n{win_msg}")
            )
            return
        state["used"].append(s)
        state["last_user"] = str(uid)
        state["last_word"] = synonym
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=info_embed("✅ Valid!", f"`{synonym}` accepted. Keep going!")
        )

    # ══════════════════════════════════════════════════════════════════════════
    # ETYMOLOGY RACE
    # ══════════════════════════════════════════════════════════════════════════

    etym_group = app_commands.Group(
        name="etymology",
        description="Etymology Race commands.",
        guild_only=True,
    )

    @etym_group.command(name="post", description="Post an Etymology Race question.")
    @game_check("etymology_race")
    async def etym_post(self, interaction: discord.Interaction):
        if not ETYMOLOGY_ANSWERS:
            await interaction.response.send_message(
                embed=warn_embed("No questions", "Etymology database is empty."), ephemeral=True
            )
            return
        word, origin = random.choice(list(ETYMOLOGY_ANSWERS.items()))
        defn = await fetch_word_definition(word)
        meaning = defn["definition"] if defn else "(definition unavailable)"
        state = {
            "word": word,
            "origin": origin,
            "meaning": meaning,
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(interaction.guild_id, "etymology_race",
                            interaction.channel_id, json.dumps(state))
        embed = info_embed(
            "🌍 Etymology Race",
            f"**Word:** `{word}`\n"
            f"**Meaning:** {meaning}\n\n"
            f"What language does this word originate from?\n"
            f"Use `/etymology answer <language>` — free text, no options!"
        )
        await interaction.response.send_message(embed=embed)

    @etym_group.command(name="answer", description="Submit the language of origin.")
    @app_commands.describe(language="The language this word comes from.")
    @game_check("etymology_race")
    async def etym_answer(self, interaction: discord.Interaction, language: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "etymology_race")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active race", "No Etymology Race is live."), ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("answered_by", []):
            await interaction.response.send_message(
                embed=warn_embed("Already tried", "You've already answered."), ephemeral=True
            )
            return
        if answers_match(language, state["origin"]):
            ms = int((time.time() - state["started_ts"]) * 1000)
            award(gid, uid, "etymology_race", POINTS["first_correct"], solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Correct origin!",
                    f"{interaction.user.mention} traced the etymology!\n"
                    f"**`{state['word']}`** comes from **{state['origin']}**\n"
                    f"**+{POINTS['first_correct']} pts**"
                )
            )
        else:
            state.setdefault("answered_by", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Wrong origin", "Not the right language!"), ephemeral=True
            )

    # ══════════════════════════════════════════════════════════════════════════
    # PORTMANTEAU PARTY
    # ══════════════════════════════════════════════════════════════════════════

    port_group = app_commands.Group(
        name="portmanteau",
        description="Portmanteau Party commands.",
        guild_only=True,
    )

    @port_group.command(name="start", description="Start a Portmanteau Party round with two concepts.")
    @app_commands.describe(concept_a="First concept.", concept_b="Second concept.")
    @game_check("portmanteau_party")
    async def port_start(self, interaction: discord.Interaction,
                         concept_a: str, concept_b: str):
        gid = interaction.guild_id
        if get_active_round(gid, "portmanteau_party"):
            await interaction.response.send_message(
                embed=warn_embed("Already active", "A Portmanteau Party is running."), ephemeral=True
            )
            return
        import datetime as dt
        expires = (dt.datetime.now(dt.timezone.utc)
                   + dt.timedelta(seconds=TIMING["portmanteau_window"])).isoformat()
        state = {
            "concept_a": concept_a,
            "concept_b": concept_b,
            "entries": {},   # user_id → {"word": .., "definition": ..}
            "votes": {},     # voter_id → entry_owner_id
        }
        create_active_round(gid, "portmanteau_party", interaction.channel_id,
                            json.dumps(state), expires)
        embed = info_embed(
            "🎉 Portmanteau Party",
            f"**Concepts:** `{concept_a}` + `{concept_b}`\n\n"
            f"Create a portmanteau word blending both concepts and define it!\n"
            f"Use `/portmanteau submit <word> <definition>`\n\n"
            f"*Voting opens with `/portmanteau vote`. Community picks the winner!*"
        )
        await interaction.response.send_message(embed=embed)

    @port_group.command(name="submit", description="Submit your portmanteau word and definition.")
    @app_commands.describe(word="Your portmanteau word.", definition="What it means.")
    @game_check("portmanteau_party")
    async def port_submit(self, interaction: discord.Interaction, word: str, definition: str):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "portmanteau_party")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Portmanteau Party is running."), ephemeral=True
            )
            return
        state = _state(round_row)
        state["entries"][uid] = {"word": word, "definition": definition}
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=win_embed("Submitted!", f"**{word}:** {definition}"), ephemeral=True
        )

    @port_group.command(name="vote", description="View and vote on portmanteau submissions.")
    @game_check("portmanteau_party")
    async def port_vote(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        round_row = get_active_round(gid, "portmanteau_party")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Portmanteau Party is running."), ephemeral=True
            )
            return
        state = _state(round_row)
        entries = state.get("entries", {})
        if not entries:
            await interaction.response.send_message(
                embed=warn_embed("No submissions yet", "Nobody has submitted a portmanteau."),
                ephemeral=True
            )
            return
        lines = []
        keys = list(entries.keys())
        for i, uid in enumerate(keys):
            e = entries[uid]
            lines.append(f"**{i+1}.** `{e['word']}` — {e['definition']} *(by <@{uid}>)*")
        embed = info_embed(
            "🎉 Portmanteau Party — Vote!",
            "\n".join(lines) + "\n\nUse `/portmanteau pick <number>` to vote!"
        )
        await interaction.response.send_message(embed=embed)

    @port_group.command(name="pick", description="Vote for your favourite portmanteau.")
    @app_commands.describe(number="The number of the entry you're voting for.")
    @game_check("portmanteau_party")
    async def port_pick(self, interaction: discord.Interaction, number: int):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "portmanteau_party")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Portmanteau Party is running."), ephemeral=True
            )
            return
        state = _state(round_row)
        keys = list(state["entries"].keys())
        idx = number - 1
        if idx < 0 or idx >= len(keys):
            await interaction.response.send_message(
                embed=warn_embed("Invalid number", f"Pick between 1 and {len(keys)}."),
                ephemeral=True
            )
            return
        if uid in state.get("votes", {}):
            await interaction.response.send_message(
                embed=warn_embed("Already voted", "You've already voted."), ephemeral=True
            )
            return
        if keys[idx] == uid:
            await interaction.response.send_message(
                embed=warn_embed("Nice try", "You can't vote for yourself."), ephemeral=True
            )
            return
        state.setdefault("votes", {})[uid] = keys[idx]
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=info_embed("Vote recorded!", f"You voted for option {number}."), ephemeral=True
        )

    @port_group.command(name="reveal", description="[Mod] Close the round and announce the winner.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("portmanteau_party")
    async def port_reveal(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        round_row = get_active_round(gid, "portmanteau_party")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Portmanteau Party is running."), ephemeral=True
            )
            return
        state = _state(round_row)
        close_active_round(round_row["id"])
        await self._resolve_portmanteau(gid, state, interaction.channel)
        await interaction.response.send_message(
            embed=info_embed("Round closed", "Results posted!"), ephemeral=True
        )

    async def _resolve_portmanteau(self, gid, state, channel):
        votes = state.get("votes", {})
        tally: dict[str, int] = {}
        for target in votes.values():
            tally[target] = tally.get(target, 0) + 1
        if not tally:
            await channel.send(embed=warn_embed("No votes", "Nobody voted — no winner."))
            return
        winner_uid = max(tally, key=lambda k: tally[k])
        entry = state["entries"].get(winner_uid, {})
        pts = POINTS["first_correct"] + tally[winner_uid]
        add_points(gid, int(winner_uid), "portmanteau_party", pts)
        await channel.send(embed=win_embed(
            "🎉 Portmanteau Party — Winner!",
            f"<@{winner_uid}>'s word wins with **{tally[winner_uid]} votes**!\n"
            f"**`{entry.get('word', '?')}`** — {entry.get('definition', '?')}\n"
            f"**+{pts} pts**"
        ))

    # ══════════════════════════════════════════════════════════════════════════
    # SPELLING BEE ROYALE  (async elimination)
    # ══════════════════════════════════════════════════════════════════════════

    bee_group = app_commands.Group(
        name="bee",
        description="Spelling Bee Royale commands.",
        guild_only=True,
    )

    @bee_group.command(name="start", description="Start a Spelling Bee Royale round.")
    @game_check("spelling_bee")
    async def bee_start(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        if get_active_round(gid, "spelling_bee"):
            await interaction.response.send_message(
                embed=warn_embed("Already active", "A Spelling Bee is running."), ephemeral=True
            )
            return
        import datetime as dt
        expires = (dt.datetime.now(dt.timezone.utc)
                   + dt.timedelta(seconds=TIMING["spelling_bee_window"])).isoformat()
        state = {
            "eliminated": [],
            "words_used": [],
            "phase": "open",
            "current_word": None,
        }
        create_active_round(gid, "spelling_bee", interaction.channel_id,
                            json.dumps(state), expires)
        word = await fetch_random_word(min_length=6)
        state["current_word"] = word
        state["words_used"].append(word)
        _save_state(get_active_round(gid, "spelling_bee")["id"], state)
        embed = info_embed(
            "🐝 Spelling Bee Royale",
            f"**Spell this word:** `{word}`\n\n"
            f"Use `/bee spell <word>` — wrong answer eliminates you from this round!\n"
            f"*Round runs over 48 hours — submit when available.*"
        )
        await interaction.response.send_message(embed=embed)

    @bee_group.command(name="spell", description="Submit your spelling.")
    @app_commands.describe(word="Spell the current word.")
    @game_check("spelling_bee")
    async def bee_spell(self, interaction: discord.Interaction, word: str):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "spelling_bee")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Spelling Bee is running."), ephemeral=True
            )
            return
        state = _state(round_row)
        if uid in state.get("eliminated", []):
            await interaction.response.send_message(
                embed=warn_embed("Eliminated", "You've been eliminated from this round."),
                ephemeral=True
            )
            return
        current = state.get("current_word", "")
        if normalise(word) == normalise(current):
            await interaction.response.send_message(
                embed=win_embed("✅ Correct spelling!", f"`{current}` — well done!"),
                ephemeral=True
            )
        else:
            state.setdefault("eliminated", []).append(uid)
            _save_state(round_row["id"], state)
            await interaction.response.send_message(
                embed=warn_embed("❌ Eliminated!", f"The correct spelling was `{current}`."),
                ephemeral=True
            )

    @bee_group.command(name="next", description="[Mod] Post the next spelling word.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("spelling_bee")
    async def bee_next(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        round_row = get_active_round(gid, "spelling_bee")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Spelling Bee is running."), ephemeral=True
            )
            return
        word = await fetch_random_word(min_length=7, max_length=14)
        if not word:
            await interaction.response.send_message(
                embed=warn_embed("API error", "Could not fetch a word."), ephemeral=True
            )
            return
        state = _state(round_row)
        state["current_word"] = word
        state["words_used"].append(word)
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=info_embed("🐝 Next word!", f"Spell: `{word}` — use `/bee spell`")
        )

    async def _resolve_spelling_bee(self, gid, state, channel):
        await channel.send(embed=info_embed(
            "🐝 Spelling Bee Royale — Round ended",
            "The Spelling Bee has concluded. Start a new one with `/bee start`."
        ))

    # ══════════════════════════════════════════════════════════════════════════
    # ACRONYM ARCHITECT
    # ══════════════════════════════════════════════════════════════════════════

    acro_group = app_commands.Group(
        name="acronym",
        description="Acronym Architect commands.",
        guild_only=True,
    )

    @acro_group.command(name="start", description="Start an Acronym Architect round.")
    @game_check("acronym_architect")
    async def acro_start(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        if get_active_round(gid, "acronym_architect"):
            await interaction.response.send_message(
                embed=warn_embed("Already active", "An Acronym Architect round is running."),
                ephemeral=True
            )
            return
        import string
        length = random.randint(4, 6)
        letters = "".join(random.choices(string.ascii_uppercase, k=length))
        import datetime as dt
        expires = (dt.datetime.now(dt.timezone.utc)
                   + dt.timedelta(seconds=TIMING["acronym_window"])).isoformat()
        state = {
            "letters": letters,
            "entries": {},   # user_id → expansion
            "votes": {},
        }
        create_active_round(gid, "acronym_architect", interaction.channel_id,
                            json.dumps(state), expires)
        embed = info_embed(
            "🔤 Acronym Architect",
            f"**Letters:** `{letters}`\n\n"
            f"Create the most convincing or funniest acronym expansion!\n"
            f"Use `/acronym submit <expansion>` — 48 hours to submit.\n"
            f"Community votes on the best entry."
        )
        await interaction.response.send_message(embed=embed)

    @acro_group.command(name="submit", description="Submit your acronym expansion.")
    @app_commands.describe(expansion="Your expansion (must start with the correct letters).")
    @game_check("acronym_architect")
    async def acro_submit(self, interaction: discord.Interaction, expansion: str):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "acronym_architect")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Acronym Architect round is running."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        # Loose check: first letters of words should match
        words = expansion.split()
        letters = state["letters"]
        initials = "".join(w[0].upper() for w in words if w)
        if initials != letters:
            await interaction.response.send_message(
                embed=warn_embed(
                    "Initials don't match",
                    f"Your expansion initials are `{initials}` but need `{letters}`."
                ),
                ephemeral=True
            )
            return
        state["entries"][uid] = expansion
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=win_embed("Submitted!", f"Your expansion: *{expansion}*"), ephemeral=True
        )

    @acro_group.command(name="vote", description="Vote on acronym submissions.")
    @game_check("acronym_architect")
    async def acro_vote(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        round_row = get_active_round(gid, "acronym_architect")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Acronym Architect round is running."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        entries = state.get("entries", {})
        if not entries:
            await interaction.response.send_message(
                embed=warn_embed("No entries", "Nobody has submitted yet."), ephemeral=True
            )
            return
        keys = list(entries.keys())
        lines = [f"**{i+1}.** {entries[k]} *(by <@{k}>)*"
                 for i, k in enumerate(keys)]
        embed = info_embed(
            f"🔤 Acronym Architect — Vote! Letters: `{state['letters']}`",
            "\n".join(lines) + "\n\nUse `/acronym pick <number>`!"
        )
        await interaction.response.send_message(embed=embed)

    @acro_group.command(name="pick", description="Pick your favourite acronym.")
    @app_commands.describe(number="The number of the entry.")
    @game_check("acronym_architect")
    async def acro_pick(self, interaction: discord.Interaction, number: int):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "acronym_architect")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Acronym Architect round is running."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        keys = list(state["entries"].keys())
        idx = number - 1
        if idx < 0 or idx >= len(keys):
            await interaction.response.send_message(
                embed=warn_embed("Invalid number", f"Pick between 1 and {len(keys)}."),
                ephemeral=True
            )
            return
        if uid in state.get("votes", {}):
            await interaction.response.send_message(
                embed=warn_embed("Already voted", "You've already voted."), ephemeral=True
            )
            return
        if keys[idx] == uid:
            await interaction.response.send_message(
                embed=warn_embed("No self-votes", "You can't vote for yourself."), ephemeral=True
            )
            return
        state.setdefault("votes", {})[uid] = keys[idx]
        _save_state(round_row["id"], state)
        await interaction.response.send_message(
            embed=info_embed("Vote recorded!", f"Voted for option {number}."), ephemeral=True
        )

    @acro_group.command(name="reveal", description="[Mod] Announce the Acronym Architect winner.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("acronym_architect")
    async def acro_reveal(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        round_row = get_active_round(gid, "acronym_architect")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Acronym Architect round is running."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        close_active_round(round_row["id"])
        await self._resolve_acronym(gid, state, interaction.channel)
        await interaction.response.send_message(
            embed=info_embed("Round closed", "Results posted!"), ephemeral=True
        )

    async def _resolve_acronym(self, gid, state, channel):
        votes = state.get("votes", {})
        tally: dict[str, int] = {}
        for t in votes.values():
            tally[t] = tally.get(t, 0) + 1
        if not tally:
            await channel.send(embed=warn_embed("No votes cast", "No winner."))
            return
        winner = max(tally, key=lambda k: tally[k])
        pts = POINTS["first_correct"] + tally[winner]
        add_points(gid, int(winner), "acronym_architect", pts)
        await channel.send(embed=win_embed(
            "🔤 Acronym Architect — Winner!",
            f"<@{winner}> wins with **{tally[winner]} votes**!\n"
            f"**{state['entries'][winner]}**\n"
            f"**+{pts} pts**"
        ))

    # ══════════════════════════════════════════════════════════════════════════
    # FORBIDDEN WORD
    # ══════════════════════════════════════════════════════════════════════════

    fw_group = app_commands.Group(
        name="fw",
        description="Forbidden Word commands.",
        guild_only=True,
    )

    @fw_group.command(name="start", description="[Mod] Start a Forbidden Word round.")
    @app_commands.describe(forbidden="The word members cannot use this round.")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("forbidden_word")
    async def fw_start(self, interaction: discord.Interaction, forbidden: str):
        gid = interaction.guild_id
        if get_active_round(gid, "forbidden_word"):
            await interaction.response.send_message(
                embed=warn_embed("Already active", "A Forbidden Word round is running."),
                ephemeral=True
            )
            return
        import datetime as dt
        expires = (dt.datetime.now(dt.timezone.utc)
                   + dt.timedelta(seconds=TIMING["forbidden_word_window"])).isoformat()
        state = {
            "forbidden": forbidden.lower(),
            "violations": {},   # user_id → count
            "clean_answers": {},
        }
        create_active_round(gid, "forbidden_word", interaction.channel_id,
                            json.dumps(state), expires)
        embed = info_embed(
            "🚫 Forbidden Word",
            f"**Forbidden word this round:** ||`{forbidden}`||\n\n"
            f"Answer trivia questions with `/fw answer` — but **do not use the forbidden word!**\n"
            f"Violations are auto-detected and penalised."
        )
        await interaction.response.send_message(embed=embed)

    @fw_group.command(name="answer", description="Submit an answer without using the forbidden word.")
    @app_commands.describe(answer="Your answer (careful with your wording!).")
    @game_check("forbidden_word")
    async def fw_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        round_row = get_active_round(gid, "forbidden_word")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active round", "No Forbidden Word round is running."),
                ephemeral=True
            )
            return
        state = _state(round_row)
        forbidden = state["forbidden"]
        if forbidden in answer.lower():
            state.setdefault("violations", {})[uid] = \
                state.get("violations", {}).get(uid, 0) + 1
            _save_state(round_row["id"], state)
            # Penalty: lose points
            add_points(gid, int(uid), "forbidden_word", -POINTS["hint_penalty"])
            await interaction.response.send_message(
                embed=warn_embed(
                    "🚫 Violation!",
                    f"You used the forbidden word! **-{POINTS['hint_penalty']} pts**"
                )
            )
        else:
            state.setdefault("clean_answers", {})[uid] = \
                state.get("clean_answers", {}).get(uid, 0) + 1
            _save_state(round_row["id"], state)
            award(gid, int(uid), "forbidden_word", POINTS["first_correct"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Clean answer!",
                    f"No forbidden word used! **+{POINTS['first_correct']} pts**"
                )
            )

    # ══════════════════════════════════════════════════════════════════════════
    # CROSSWORD CLUE CLASH
    # ══════════════════════════════════════════════════════════════════════════

    cwc_group = app_commands.Group(
        name="cwc",
        description="Crossword Clue Clash commands.",
        guild_only=True,
    )

    @cwc_group.command(name="post",
                       description="[Mod] Post a cryptic crossword clue.")
    @app_commands.describe(clue="The cryptic clue.", answer="The correct answer (hidden).")
    @app_commands.default_permissions(manage_messages=True)
    @game_check("crossword_clue")
    async def cwc_post(self, interaction: discord.Interaction, clue: str, answer: str):
        gid = interaction.guild_id
        state = {
            "clue": clue,
            "answer": answer,
            "answered_by": [],
            "started_ts": time.time(),
        }
        create_active_round(gid, "crossword_clue", interaction.channel_id, json.dumps(state))
        embed = info_embed(
            "✏️ Crossword Clue Clash",
            f"**Clue:** {clue}\n\n"
            f"*No enumeration given — you have to work out the word length too!*\n"
            f"Use `/cwc answer <word>` to submit!"
        )
        await interaction.response.send_message(embed=embed)

    @cwc_group.command(name="answer", description="Submit your crossword answer.")
    @app_commands.describe(answer="Your answer word.")
    @game_check("crossword_clue")
    async def cwc_answer(self, interaction: discord.Interaction, answer: str):
        gid = interaction.guild_id
        uid = interaction.user.id
        round_row = get_active_round(gid, "crossword_clue")
        if not round_row:
            await interaction.response.send_message(
                embed=warn_embed("No active clue", "No Crossword Clue is live."), ephemeral=True
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
            award(gid, uid, "crossword_clue", POINTS["first_correct"], solve_ms=ms)
            close_active_round(round_row["id"])
            await interaction.response.send_message(
                embed=win_embed(
                    "✅ Correct!",
                    f"{interaction.user.mention} cracked the clue!\n"
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


async def setup(bot: commands.Bot):
    await bot.add_cog(WordplayCog(bot))
