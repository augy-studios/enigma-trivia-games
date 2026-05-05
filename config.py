"""
config.py — Central configuration for Enigma bot.
Edit values here or override via environment variables.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Bot ──────────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
BOT_PREFIX: str = "!"          # fallback prefix (slash commands are primary)

# ── Database ─────────────────────────────────────────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "enigma.db")

# ── Scheduler ────────────────────────────────────────────────────────────────
SCHEDULER_INTERVAL_SECONDS: int = 60   # how often the scheduler loop ticks

# ── Point values ─────────────────────────────────────────────────────────────
POINTS = {
    "first_correct":        10,
    "subsequent_correct":    5,
    "stumper_win":          15,   # question stumped server for 48 h
    "solver_win":           10,
    "hint_penalty":          3,   # deducted per extra hint used
    "escalating_max":       20,   # points if solved with 0 hints used
    "chain_per_link":        5,
    "collective_iq_share":   4,   # per contributor in Collective IQ
    "definition_duel_fool":  5,   # per person fooled
    "definition_duel_spot":  5,   # spotting the real definition
    "vote_correct_bonus":    3,   # bonus for fastest among correct in T/T
    "logic_lock_max":       20,
    "logic_lock_decay":      2,   # per correct answer submitted after first
    "speed_bonus":           5,
    "category_king":         8,
    "fermi_winner":         10,
    "price_is_right":       10,
    "probability_pulse":    10,
    "timeline_winner":      10,
    "blurred_max":          20,
    "blurred_decay":         4,   # per reveal shown
    "examiner_stump":        5,
    "fact_drop_quality":     2,   # per upvote on a submitted fact
    "contributor":           1,   # per approved submission
}

# ── API endpoints (all free, no key required) ─────────────────────────────────
API = {
    "trivia":       "https://opentdb.com/api.php",           # Open Trivia DB
    "quotes":       "https://api.quotable.io/random",        # Quotable
    "words":        "https://api.datamuse.com/words",        # Datamuse
    "word_def":     "https://api.dictionaryapi.dev/api/v2/entries/en/",  # Free Dict
    "numberfacts":  "http://numbersapi.com/",                 # Numbers API
    "word_random":  "https://random-word-api.herokuapp.com/word",
    "facts":        "https://uselessfacts.jsph.pl/random.json?language=en",
    "estimation":   None,  # generated internally
}

# ── Timing (seconds unless noted) ────────────────────────────────────────────
TIMING = {
    "stump_post_interval":      86400,   # 24 h between Stump the Server posts
    "stump_solver_window":      172800,  # 48 h to solve before stumper earns point
    "escalating_hint_interval": 21600,   # 6 h between hints
    "logic_lock_window":        172800,  # 48 h
    "definition_duel_window":   172800,
    "portmanteau_window":       172800,
    "spelling_bee_window":      172800,
    "acronym_window":           172800,
    "forbidden_word_window":    172800,
    "fact_drop_interval":       86400,
    "blurred_reveal_interval":  1800,    # 30 min between reveals
    "examiner_window":          172800,
}

# ── Cog registration ──────────────────────────────────────────────────────────
# Map game_key → human-readable name (used in /game enable/disable)
GAME_REGISTRY: dict[str, str] = {
    # Trivia
    "classic_trivia":       "Classic Trivia",
    "stump_server":         "Stump the Server",
    "category_king":        "Category King",
    "escalating_enigma":    "Escalating Enigma",
    "chain_reaction":       "Chain Reaction Trivia",
    "collective_iq":        "Collective IQ",
    # Specialist
    "niche_gauntlet":       "Niche Gauntlet",
    "deep_cut":             "Deep Cut",
    "true_or_truffle":      "True or Truffle",
    "source_check":         "Source Check",
    # Wordplay
    "anagram_arena":        "Anagram Arena",
    "definition_duel":      "Definition Duel",
    "synonym_sprint":       "Synonym Sprint",
    "etymology_race":       "Etymology Race",
    "portmanteau_party":    "Portmanteau Party",
    "spelling_bee":         "Spelling Bee Royale",
    "acronym_architect":    "Acronym Architect",
    "forbidden_word":       "Forbidden Word",
    "crossword_clue":       "Crossword Clue Clash",
    # Logic
    "logic_lock":           "Logic Lock",
    "pattern_breaker":      "Pattern Breaker",
    "missing_link":         "The Missing Link",
    "contradiction":        "Contradiction Spotter",
    "inference_engine":     "Inference Engine",
    # Strategy
    "fermi_estimator":      "Fermi Estimator",
    "price_is_right":       "Price Is Right",
    "probability_pulse":    "Probability Pulse",
    "timeline_toss":        "Timeline Toss",
    # Visual
    "blurred_vision":       "Blurred Vision",
    "macro_world":          "Macro World",
    "map_surgeon":          "Map Surgeon",
    "silhouette_showdown":  "Silhouette Showdown",
    "colour_coded":         "Colour Coded",
    # Pop culture
    "logo_blitz":           "Logo Blitz",
    "opening_line":         "Opening Line",
    "character_silhouette": "Character Silhouette",
    # Member-generated
    "question_curator":     "Question Curator",
    "fact_drop":            "Fact Drop",
    "the_examiner":         "The Examiner",
}
