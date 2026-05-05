# Enigma 🧩

A feature-rich Discord.py slash-commands bot with 35+ knowledge and puzzle games, full per-guild configuration, and a multi-dimensional leaderboard system. Runs on a Debian VPS with SQLite for storage and scheduling.

---

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the Bot](#running-the-bot)
- [Admin Commands](#admin-commands)
- [Game List](#game-list)
- [Leaderboards](#leaderboards)
- [External APIs](#external-apis)
- [Project Structure](#project-structure)
- [Development Notes](#development-notes)

---

## Features

- **35+ games** across Trivia, Word & Language, Logic, Visual, and Member-Generated categories
- **Per-guild, per-game** enable/disable and optional channel restriction
- **SQLite-backed** storage and scheduler — no Redis or external broker required
- **Multi-dimensional leaderboards**: all-time points, category, speed, stumper, calibration, streak, and contributor boards
- **Free open-source REST APIs** for trivia, quotes, words, facts, and numbers — no paid keys needed
- **discord.py slash commands** throughout — no legacy prefix commands exposed to users

---

## Prerequisites

- Python **3.11+**
- A Discord bot application with `MESSAGE CONTENT` and `SERVER MEMBERS` intents enabled
- Debian 13 VPS (or any Linux host) with internet access
- `tmux` recommended for persistent background execution
- `git` for version control

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/your-username/enigma.git
cd enigma

# 2. Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and edit the environment file
cp .env.example .env
nano .env   # set BOT_TOKEN (and optionally DB_PATH)
```

---

## Configuration

Edit `.env`:

```env
BOT_TOKEN=your_discord_bot_token_here
DB_PATH=enigma.db          # optional, defaults to enigma.db in the working directory
```

In the Discord Developer Portal:
- Enable **Server Members Intent** and **Message Content Intent** under *Bot → Privileged Gateway Intents*
- Invite the bot with scopes: `bot`, `applications.commands`
- Required permissions: `Send Messages`, `Embed Links`, `Read Message History`, `Manage Messages` (for mod actions)

---

## Running the Bot

### Directly

```bash
source venv/bin/activate
python bot.py
```

### In tmux (recommended for VPS)

```bash
tmux new-session -s enigma
source venv/bin/activate
python bot.py
# Detach: Ctrl+B then D
# Reattach later: tmux attach -t enigma
```

### Auto-restart wrapper (optional)

```bash
while true; do python bot.py; echo "Restarting in 5s…"; sleep 5; done
```

---

## Admin Commands

All admin commands require **Manage Guild** permission.

| Command | Description |
| --- | --- |
| `/game enable <game>` | Enable a game in this server |
| `/game disable <game>` | Disable a game in this server |
| `/game channel <game> <channel>` | Restrict a game to a specific channel |
| `/game list` | List all games and their status |
| `/game status <game>` | Show config for a specific game |

---

## Game List

### Trivia & Facts

| Command Group | Game | Description |
| --- | --- | --- |
| `/trivia` | **Classic Trivia** | Open bounty — always one live question, first correct answer wins |
| `/stump` | **Stump the Server** | Member-submitted questions posted every 24h, dual stump/solve leaderboard |
| `/categoryking` `/ckanswer` | **Category King** | Category-tagged trivia with per-category leaderboards |
| `/enigma` | **Escalating Enigma** | Riddle with hints dropping every 6h; fewer hints = more points |
| `/chain` | **Chain Reaction** | Correct answers unlock the next question; wrong answers break the chain |
| `/ciq` | **Collective IQ** | Cooperative — members pool partial answers in thread |

### Specialist Knowledge

| Command Group | Game | Description |
| --- | --- | --- |
| `/niche` | **Niche Gauntlet** | Unlabelled expert questions; guess the field AND the answer |
| `/deepcut` `/dcanswer` | **Deep Cut** | Obscure corners of well-known topics |
| `/tot` | **True or Truffle** | Vote true/false on suspicious-sounding statements |
| `/source` | **Source Check** | Identify who said a quote — free text, no multiple choice |

### Word & Language

| Command Group | Game | Description |
| --- | --- | --- |
| `/anagram` | **Anagram Arena** | Unscramble words; leaderboard tracks wins and solve time |
| `/defduel` | **Definition Duel** | Submit fake definitions; vote for the real one |
| `/synonym` | **Synonym Sprint** | Race to submit unique synonyms — last valid one wins |
| `/etymology` | **Etymology Race** | Identify a word's language of origin |
| `/portmanteau` | **Portmanteau Party** | Blend two concepts; community votes on best result |
| `/bee` | **Spelling Bee Royale** | Async spelling elimination over 48h |
| `/acronym` | **Acronym Architect** | Expand a random letter string into a convincing acronym |
| `/fw` | **Forbidden Word** | Answer trivia without using the designated forbidden word |
| `/cwc` | **Crossword Clue Clash** | Cryptic clue, no enumeration — first correct answer wins |

### Logic & Reasoning

| Command Group | Game | Description |
| --- | --- | --- |
| `/ll` | **Logic Lock** | Grid and lateral thinking puzzles; diminishing points for later solvers |
| `/pb` | **Pattern Breaker** | Find the anomaly in a sequence and explain why |
| `/link` | **The Missing Link** | Find the word connecting three unrelated words |
| `/cs` | **Contradiction Spotter** | Find the logical contradiction hidden in a paragraph |
| `/inf` | **Inference Engine** | Deduce the answer from 3–5 clues; fewer clues = more points |
| `/fermi` | **Fermi Estimator** | Estimate quantities; closest wins |
| `/price` | **Price is Right** | Closest without going over |
| `/prob` | **Probability Pulse** | Estimate real-world probabilities; calibration tracked over time |
| `/tl` | **Timeline Toss** | Guess the year of a historical event; year-distance tracked |

### Visual & Media

| Command Group | Game | Description |
| --- | --- | --- |
| `/blurred` | **Blurred Vision** | Pixelated image clears every 30 min; fewer reveals = more points |
| `/macro` | **Macro World** | Extreme close-up of an everyday object |
| `/map` | **Map Surgeon** | Cropped, unlabelled map section — identify the location |
| `/silhouette` | **Silhouette Showdown** | Animal, landmark, or object silhouette |
| `/colour` | **Colour Coded** | Hex palette of a logo, flag, or artwork — no other context |
| `/logo` | **Logo Blitz** | Cropped or modified logo — identify the brand |
| `/ol` | **Opening Line** | First line of a book, film, game, or song |
| `/char` | **Character Silhouette** | Fictional character silhouette — identify character and source |

### Member-Generated Content

| Command Group | Game | Description |
| --- | --- | --- |
| `/qc` | **Question Curator** | Submit, upvote, and approve questions into the active pool |
| `/fd` | **Fact Drop** | Submit facts with sources; bot posts daily; community votes |
| `/exam` | **The Examiner** | Rotating examiner posts 5 questions; earns points for stumps |

---

## Leaderboards

All accessible via `/leaderboard`:

| Sub-command | Tracks |
| --- | --- |
| `/leaderboard overall` | All-time points across every game |
| `/leaderboard game <game>` | Points for a specific game |
| `/leaderboard category <name>` | Category-specific points (Category King) |
| `/leaderboard speed` | Average solve time for correct answers |
| `/leaderboard stumper` | Whose submitted questions stumped the most people |
| `/leaderboard calibration` | Estimation accuracy over time (Fermi, Probability Pulse) |
| `/leaderboard contributor` | Most questions, facts, and content submitted |
| `/leaderboard streak` | Longest current correct-answer streak |
| `/leaderboard best_streak` | All-time longest streaks |
| `/leaderboard me` | Your personal stats |

---

## External APIs

All APIs are free and require no authentication key:

| API | Used for |
| --- | --- |
| [Open Trivia DB](https://opentdb.com/api.php) | Classic Trivia, Category King, Deep Cut |
| [Quotable](https://api.quotable.io/random) | Source Check |
| [Datamuse](https://api.datamuse.com) | Synonyms (Synonym Sprint), rhymes |
| [Free Dictionary API](https://api.dictionaryapi.dev) | Definitions, Etymology Race |
| [Random Word API](https://random-word-api.herokuapp.com) | Anagram Arena, Spelling Bee, Acronym Architect |
| [Useless Facts](https://uselessfacts.jsph.pl/random.json) | Fact Drop seed content |
| [Numbers API](http://numbersapi.com) | Timeline Toss, Fermi Estimator |

---

## Project Structure

```bash
enigma/
├── bot.py                   # Entry point — bot init, cog loading, slash sync
├── config.py                # Tokens, API URLs, point values, timing, game registry
├── database.py              # SQLite schema + all DB helper functions
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── utils/
│   ├── api.py               # Async wrappers for all external REST APIs
│   └── scoring.py           # Embeds, game_check decorator, award(), answers_match()
└── cogs/
    ├── admin.py             # /game enable|disable|channel|list|status
    ├── leaderboard.py       # /leaderboard *
    ├── trivia/
    │   └── classic.py       # Classic Trivia, Stump the Server, Category King,
    │                        # Escalating Enigma, Chain Reaction, Collective IQ
    ├── specialist/
    │   └── specialist.py    # Niche Gauntlet, Deep Cut, True or Truffle, Source Check
    ├── wordplay/
    │   └── wordplay.py      # Anagram Arena, Definition Duel, Synonym Sprint,
    │                        # Etymology Race, Portmanteau Party, Spelling Bee,
    │                        # Acronym Architect, Forbidden Word, Crossword Clue Clash
    ├── logic/
    │   └── logic.py         # Logic Lock, Pattern Breaker, Missing Link,
    │                        # Contradiction Spotter, Inference Engine,
    │                        # Fermi Estimator, Price is Right,
    │                        # Probability Pulse, Timeline Toss
    ├── visual/
    │   └── visual.py        # Blurred Vision, Macro World, Map Surgeon,
    │                        # Silhouette Showdown, Colour Coded, Logo Blitz,
    │                        # Opening Line, Character Silhouette
    └── member/
        └── member.py        # Question Curator, Fact Drop, The Examiner
```

---

## Development Notes

- **Slash command sync** happens globally on startup — propagation can take up to an hour on first deploy. Force a guild-specific sync in `setup_hook` with `self.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))` for instant testing.
- **SQLite scheduling** — the scheduler polls `scheduled_posts` every 60 seconds. Sub-minute precision is not guaranteed.
- **Image-based games** (Blurred Vision, Macro, etc.) require moderators to post image URLs manually. The bot does not generate or host images.
- **Answer matching** uses `difflib.SequenceMatcher` with a 0.85 similarity threshold after normalisation (lowercase, strip non-alphanumeric). Adjust `ANSWER_THRESHOLD` in `utils/scoring.py` if needed.
- **Point values** are all centralised in `config.py → POINTS`. Edit there to rebalance without touching cog code.
- **Game timing** (hint intervals, round durations, etc.) is in `config.py → TIMING` (in seconds).
