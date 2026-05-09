# dutch-srs-training

A small, local-first spaced repetition app for learning Dutch from the top 1000 most frequent lemmas in conversational subtitle data. Anki-style scheduling, one example sentence per word, an English gloss on both word and sentence, and three drilling surfaces: New words, Sentence forming, Word order. Audio is included.

It runs as a Flask web app on `http://localhost:5051`. There is also a terminal review mode if you prefer keys to clicks. Your review history lives in a single SQLite file under your OS user-data folder, so the project tree stays clean and survives a `git pull -f` without losing progress.

I built this for myself because Anki's deck library is too broad and Duolingo is too gamified for what I needed, the top 1000 lemmas of conversational Dutch ranked by frequency, each anchored to one canonical sentence, with sense labels for polysemes. If you are cloning this, you get a deck that works on day one.

## What it does

You see a Dutch headword and a single Dutch sentence. You press space, you see the English gloss, every labeled sense, and any pedagogy notes. You grade with `1` again, `2` hard, `3` good, `4` easy. The DB writes after every card.

Three views, all in the same single-page UI:

1. **New words.** Standard SRS. Due cards first, then up to N new from the frequency-ranked queue. SM-2 with 1 minute and 10 minute learning steps, 1 day graduating interval, ease floor 1.3, max interval 5 years, defaults that match Anki.

2. **Sentence forming.** Production drill. You see the English, attempt the Dutch in your head, press space to reveal. Then six pills appear, one per variation axis (past, future, statement, question, verb-swap, noun-swap, person-swap). Two of the six are tense alternatives picked from the original's tense, the rest are lexical or form-level variations. Click a pill, get the slightly-shifted English, attempt the Dutch again. This is for generalization, not single-phrase memorization.

3. **Word order.** Drag-and-drop drill on Dutch syntax. The Dutch sentence is rearranged into English word order (the literal gloss). You drag the tokens back into the canonical Dutch arrangement and press Check. Pass locks the row green. Fail surfaces a one-line structural hint (V2 inversion, subordinate verb final) and marks the misplaced tokens.

A fourth tab, **Custom**, lets you push a sentence into the deck without editing source. Useful when you hear something in the wild and want it as a card immediately.

## Principles

**Local-first, no account, no telemetry.** The whole app runs against a SQLite file on your machine. There is no signup, no sync server, no analytics. Progress does not roam between devices automatically. If you want it to roam, point `DUTCH_SRS_DB` at a path inside a synced folder that handles SQLite well (Syncthing, a private git-annex repo). iCloud and Dropbox can corrupt SQLite WAL files, do not point the DB there.

**Frequency, not curriculum.** The deck is the top 1000 lemmas from Hermit Dave's OpenSubtitles list, lemmatized via spaCy so that `ben`, `is`, `was`, `zijn`, `geweest` collapse onto the lemma `zijn`. Subtitles approximate spoken register, which is what an A1-A2 learner meets in conversation. Ranks 1 to 200 ship with full sentences and audio. Ranks 201 to 1000 sit dormant in `data/words.csv` until sentences are written for them.

**One canonical sentence per sense.** Polysemous words carry one sentence per A1-A2 sense, with a sense label. The card front shows only the primary sentence so recall is anchored to one usage at a time. The back lists every sense so the meaning landscape is visible on each review. Words like `nog` (still / yet / more / another) and `er` (existential / locative / partitive) are handled this way.

**Same-data, different drill.** New words, Sentence forming, and Word order all read from the same words and sentences tables. There is no separate "Word order deck", no parallel curriculum to maintain. A sentence with a `literal_gloss` becomes available in Word order the moment its headword is in `learning`, `review`, or `relearning`. New words always uses the canonical Dutch sentence, never a variation.

**SM-2 with steps, not FSRS.** I copied Anki's algorithm rather than the newer FSRS. SM-2 is small (200 lines), deterministic, and easy to debug from the `review_log` table. FSRS needs a few hundred personal reviews before it tunes well. The four-button grade is the only signal either algorithm consumes, so swapping in FSRS later is a focused change in `srs.py`.

**Append-only logs, no rewrites.** Confusion notes, vocab logs, daily summaries: every writer in `vault.py` only appends. The chronological history of where you got stuck stays intact, so an LLM can later look at it and explain what was confusing without first reconstructing a timeline.

**Loud failures over silent fallbacks.** A missing literal gloss makes the sentence skip the Word order queue, it does not auto-generate a fake one. A missing audio file makes the play button hide, it does not silently fall back to browser TTS.

## Run it on your machine

Requires Python 3.10 or newer. Flask is installed from `requirements.txt`, everything else is in the standard library.

### macOS and Linux

```bash
git clone https://github.com/<your-user>/dutch-srs-training.git
cd dutch-srs-training
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./dutch web
```

### Windows (PowerShell)

```powershell
git clone https://github.com/<your-user>/dutch-srs-training.git
cd dutch-srs-training
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
.\dutch.cmd web
```

The first run on any machine seeds the SQLite database from `data/words.csv` and `app/seed_data.py`, so you do not need a separate setup step. Your browser will open to `http://localhost:5051`.

A useful shell alias on macOS or Linux, drop this in `~/.zshrc` or `~/.bashrc` and you can run `dutch web` from any directory:

```bash
alias dutch='cd ~/path/to/dutch-srs-training && source .venv/bin/activate && ./dutch'
```

### Common commands

- `./dutch web` opens the web UI with default 20 new cards per session.
- `./dutch web --new 30` raises the cap for one session.
- `./dutch stats` prints deck size, due count, and per-state breakdown.
- `./dutch review` runs a terminal review session, same logic as the web UI.
- `./dutch add fiets --article de --english "bike" --sentence "Ik pak mijn fiets." --sentence-en "I grab my bike."` adds a one-off card.
- `python3 app/test_srs.py` runs the algorithm tests.

## File structure

```
dutch-srs-training/
  README.md              you are here
  AGENTS.md              short brief for LLMs that clone this repo
  CONTRIBUTING.md        how to add words, sentences, glosses, send a PR
  MAINTENANCE.md         release plan, issue triage, how the deck grows
  LICENSE                MIT
  requirements.txt       Flask, that is all
  requirements-dev.txt   spaCy and Google TTS, only for extending the deck
  dutch                  bash launcher (macOS, Linux)
  dutch.cmd              cmd launcher (Windows)
  app/
    cli.py               argparse subcommands: seed, review, stats, web, add
    web.py               Flask backend, JSON API for the single-page UI
    static/
      index.html         the single-page frontend, embedded CSS and JS
      audio/words/       per-lemma MP3s
      audio/sentences/   per-sentence MP3s
    srs.py               SM-2 algorithm, no I/O, no DB
    db.py                SQLite persistence, schema migrations
    schema.sql           tables: words, sentences, sentence_variations,
                         reviews, review_log, word_order_attempts, settings
    seed_data.py         glosses, sentences, variations, literal glosses
    vault.py             optional integration with an external markdown vault
    tts_audio.py         optional, generates MP3s via Google Cloud TTS
    build_wordlist.py    optional, rebuilds words.csv from raw subtitle data
    build_glosses.py     optional, helper for authoring literal glosses
    test_srs.py          unit tests for the algorithm
  data/
    words.csv            top 1000 lemmas with rank, POS, frequency
  logs/                  reserved for future export dumps, kept empty
```

## Where progress lives

Two locations, on purpose.

**Code, content, and audio live inside the repo.** `seed_data.py` is the canonical source for words, sentences, glosses, and variations. `data/words.csv` is the frequency-ranked catalog. `app/static/audio/` holds the MP3s. All under version control.

**Your review history lives outside the repo.** SQLite at one of these paths, depending on your OS:

- macOS: `~/Library/Application Support/dutch-srs/srs.db`
- Linux: `~/.local/share/dutch-srs/srs.db` (respects `XDG_DATA_HOME`)
- Windows: `%LOCALAPPDATA%\dutch-srs\srs.db`

Override with `DUTCH_SRS_DB=/some/path/srs.db` if you want it elsewhere.

The split exists so the repo can be a fixed deck definition while your SQLite file is the only thing that changes as you study. You can delete the repo, clone a fresh one, and your progress is still there. You can update the seed (new words, fixed glosses, more sentences) and your existing reviews stay intact, the migration in `db.init_db` is forward-only and additive.

The SQLite schema:

| Table | What it holds |
| --- | --- |
| `words` | One row per lemma. `rank`, `pos`, `article`, `english`, `audio_path`. |
| `sentences` | One row per sentence. Foreign key to `words`. `nl`, `en`, `sense`, `tense`, `form`, `literal_gloss`. |
| `sentence_variations` | Six rows per primary sentence. The near-variations for Sentence forming. |
| `reviews` | One row per word card. SM-2 state: `state`, `due_at`, `interval_days`, `ease`, `lapses`, `reps`. |
| `review_log` | One row per grade. The full history with `prev_card_json` for undo. |
| `word_order_attempts` | One row per Word order submission. Submitted order, canonical, ok flag. |
| `settings` | Singleton row of per-deck settings. |

Migrations are forward-only column adds inside `db.init_db`. There is no migration framework, the schema is too small to justify Alembic. If you add a column, follow the existing pattern: `PRAGMA table_info`, check, `ALTER TABLE` if missing.

## Optional integrations

**External markdown vault.** Set `DUTCH_SRS_VAULT=/path/to/notes` and the app will append three things to that folder: a row to `Vocab/Vocab Log.md` for every new word you graduate, a one-line summary to `Daily/YYYY-MM-DD.md` if today's daily note exists, and confusion notes you write in the UI to `SRS/Feedback Log.md`. Without this var, none of these writers run, and the API endpoints stay functional. I use this to integrate with my Obsidian vault. The file layout is documented in `app/vault.py`.

**Audio.** The repo ships per-lemma MP3s and per-sentence MP3s, all keyed by stable identifiers (lemma string for words, sha1(dutch_text)[:16] for sentences). Both work on first run, no Google Cloud key required.

To extend the audio set when you add new sentences, you do need a Google Cloud Text-to-Speech key. Set `GOOGLE_APPLICATION_CREDENTIALS` to the JSON path, then:

```bash
python app/tts_audio.py --scope words --dry-run        # preview
python app/tts_audio.py --scope words                  # render new lemmas
python app/tts_audio.py --scope sentences              # render new sentences
```

The script is idempotent, it skips items that already have an MP3 unless you pass `--force`. See the docstring at the top of `app/tts_audio.py` for the one-time GCP setup.

**Wordlist regeneration.** `build_wordlist.py` rebuilds `data/words.csv` from a raw OpenSubtitles dump. You only need this if the source list updates upstream, which is rare. Needs spaCy and `nl_core_news_sm`.

**Timezone.** Set `DUTCH_SRS_TZ=Europe/Amsterdam` or your local IANA zone. This only affects the heatmap day-bucketing, the SM-2 scheduler is timezone-agnostic and runs on UTC.

## How to extend the deck

The bottleneck is writing new sentences. Open `app/seed_data.py`. Each entry looks like this:

```python
'fiets': {
    'article': 'de',
    'english': 'bike',
    'level': 'A1',
    'sentences': [
        {
            'nl': 'Morgen ga ik naar de markt op de fiets.',
            'en': 'Tomorrow I am going to the market on the bike.',
            'literal_gloss': 'Morgen ik ga naar de markt op de fiets',
            'tense': 'present',
            'form': 'statement',
            'sense': None,
            'variations': [
                {'nl': '...', 'en': '...', 'varied': 'past'},
                {'nl': '...', 'en': '...', 'varied': 'future'},
                {'nl': '...', 'en': '...', 'varied': 'question'},
                {'nl': '...', 'en': '...', 'varied': 'verb'},
                {'nl': '...', 'en': '...', 'varied': 'noun'},
                {'nl': '...', 'en': '...', 'varied': 'person'},
            ],
        },
    ],
},
```

The `literal_gloss` field must be a permutation of the canonical Dutch tokens, same words, possibly reordered, no additions or substitutions. The seeder, the Custom endpoint, and the CLI all enforce this. If you want a sentence to skip the Word order drill, leave `literal_gloss` empty.

Once you save the file, run `./dutch seed` to push your changes into the SQLite. The seeder is idempotent, your existing review state is preserved.

For a one-off card without editing source, use `./dutch add` or the Custom tab in the web UI.

## How to verify a change

```bash
python3 app/test_srs.py                             # algorithm unit tests
./dutch stats                                       # smoke test
./dutch seed                                        # idempotent reseed
./dutch web                                         # eyeball the three views
```

If you change `srs.py`, run the unit tests. If you change the schema, delete your local DB and reseed (the auto-seed on first run will rebuild it). If you change the frontend, hard-reload. The static cache is disabled in `web.py`.

## What this project is not

Not a Duolingo replacement. No streaks, XP, mascots, or leaderboards.

Not a course. It does not teach grammar. It assumes you have grammar reference materials elsewhere and uses sentences to drill the words those rules act on.

Not multi-user. The Flask server is a single-process, single-user development server. Do not deploy it on the public internet, there is no auth.

## Help, contributing, bugs

Open an issue on GitHub. For changes, see [CONTRIBUTING.md](CONTRIBUTING.md). For the maintenance plan and how the deck grows over time, see [MAINTENANCE.md](MAINTENANCE.md). LLMs working in this codebase should read [AGENTS.md](AGENTS.md) first.

## License

MIT, see [LICENSE](LICENSE).
