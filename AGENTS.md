# AGENTS.md

A focused brief for LLM-driven coding tools (Claude Code, Cursor, Aider, Codex CLI, anything else that ingests this file). The human-facing readme is `README.md`. This file points you at what matters and tells you what to leave alone.

## Read first

1. `README.md` for the why and the run commands.
2. `app/srs.py` for the SM-2 algorithm. Pure functions, no I/O, easy to reason about.
3. `app/db.py` for the SQLite layer. Schema migrations live in `init_db`, additive only.
4. `app/web.py` for the API surface. Each route has a docstring describing input and output shape.
5. `app/seed_data.py` for the deck definition. Large file, scan rather than read.

You probably do not need to read `static/index.html` end-to-end. It is a 5000-line single-page UI, scan with `grep` for the symbol you care about.

## What you can change without asking

- Add or correct a sentence in `app/seed_data.py`. Run `./dutch seed` afterwards.
- Add a literal gloss to an existing sentence (must be a permutation of the canonical Dutch tokens).
- Add a unit test in `app/test_srs.py`.
- Improve a docstring or a code comment.
- Fix a typo in any markdown file.

## What you should ask before touching

- The SM-2 parameters in `app/srs.py` (learning steps, ease floor, intervals).
- The schema in `app/schema.sql`. If you add a column, also add the forward-only `ALTER TABLE` in `db.init_db`. Never alter or drop a column in code, write a one-off migration script under `app/migrations/` and document it.
- The API contract in `app/web.py`. The frontend in `static/index.html` is tightly coupled to it.
- Anything in `data/words.csv`. The ranks come from a frequency analysis, do not hand-edit them.

## Hard rules

- **No emojis** in code, comments, or markdown. Anywhere.
- **No emdashes** in markdown. Use commas, full stops, or parentheses.
- **No silent fallbacks.** If data is missing, the UI hides the affected control. Do not invent placeholder data.
- **Do not commit** the SQLite DB, audio renders that you generated locally with a credentialed key, or any file containing API keys.
- **Do not change** the location of the SQLite DB. Users have data there.

## Run loop for verifying a change

```bash
python3 app/test_srs.py
./dutch seed
./dutch stats
./dutch web
```

If your change touches the schema, also do this in a scratch shell:

```bash
DUTCH_SRS_DB=/tmp/test-srs.db ./dutch seed
DUTCH_SRS_DB=/tmp/test-srs.db ./dutch stats
rm /tmp/test-srs.db
```

That round-trips a full seed against an empty DB, catches most schema breakage.

## Where state lives

| Concern | Location |
| --- | --- |
| Deck definition | `app/seed_data.py`, `data/words.csv` |
| Audio assets | `app/static/audio/{words,sentences}/` |
| Algorithm | `app/srs.py` |
| Persistence | `app/db.py`, schema in `app/schema.sql` |
| Per-user review history | SQLite outside the repo, see README "Where progress lives" |
| Optional markdown side-effects | `app/vault.py`, gated on `DUTCH_SRS_VAULT` |

## How the queues are built

`/api/next` rebuilds the queue every call: due learning and relearning cards first, then due review cards, then up to N from the new pool until the daily new cap is hit. `_session_new_seen` is a module-level counter that survives across page loads in the same process. The new cap defaults to 20, set by `--new` on the CLI.

## Pitfalls

- **iCloud and Dropbox.** Do not point `DUTCH_SRS_DB` inside a folder synced by either. WAL files corrupt. Syncthing is fine.
- **Static cache.** `web.py` sets `SEND_FILE_MAX_AGE_DEFAULT = 0` so frontend edits show on reload. Keep it that way during development.
- **Static cache, again.** Browsers still cache. Hard-reload (Cmd-Shift-R or Ctrl-Shift-R) when in doubt.
- **`literal_gloss` invariant.** Same multiset of tokens as the canonical Dutch. Tokenization matches `web._tokenize_for_word_order`. If your gloss looks correct but Word order rejects it, your tokens probably differ in punctuation or casing.
- **Variations are exactly six.** The pill UI assumes six variations per primary sentence (two tense alternatives, one form alternative, three lexical swaps). Do not author seven or four. If you need to retire a variation, replace it.
- **Migrations are forward-only and additive.** Existing users will run your code against an old schema. Always probe with `PRAGMA table_info` before assuming a column exists.

## Common tasks, by example

**Add a one-off card from the CLI.**

```bash
./dutch add fiets --article de --english "bike" \
    --sentence "Morgen ga ik naar de markt op de fiets." \
    --sentence-en "Tomorrow I am going to the market on the bike." \
    --gloss "Morgen ik ga naar de markt op de fiets"
```

**Backfill audio for a single new lemma.** Requires `GOOGLE_APPLICATION_CREDENTIALS`.

```bash
python app/tts_audio.py --lemma fiets --force
```

**Audio key conventions.** Word MP3s are named after the lemma string,
`audio/words/<lemma>.mp3`. Sentence MP3s are named after a stable hash of
the canonical Dutch text, `audio/sentences/<sha1(dutch)[:16]>.mp3`, see
`cli.sentence_audio_hash`. Do not key sentence audio by SQLite id, the id
is unstable across fresh seeds and across machines.

**Reseed a corrupted DB without losing reviews.** Reviews live in the `reviews` and `review_log` tables, the seeder only touches `words`, `sentences`, and `sentence_variations`. So:

```bash
./dutch seed
```

If the DB itself is unreadable, delete it. The auto-seed on first run rebuilds the deck. Reviews are gone, but the deck is back.

## Style

Top-level docstring on every module, one-line summary then a longer block. Public functions get an Args / Returns / Raises block where applicable. Comments explain why, not what. No marketing language.
