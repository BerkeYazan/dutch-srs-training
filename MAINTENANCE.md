# Maintenance plan

This is the living document for how the project grows. Implementation roadmap, release cadence, issue triage, deck growth, decision log. The intent is that anyone (myself in six months, a contributor, an LLM, a friend who forks) can pick up the project and know what is happening, what is next, and where the rationale for past calls is recorded.

## Status, today

- Ranks 1 to 200 are fully seeded with sentences, literal glosses, six variations each, and audio.
- Ranks 201 to 1000 sit dormant in `data/words.csv`. They are visible in the catalog but do not enter the new queue until they get a sentence in `seed_data.py`.
- The web UI is the default surface. The terminal CLI is kept as a fallback and for one-off card adds.
- Algorithm: SM-2 with Anki defaults. Stable, no plans to switch.
- Audio: 200 word MP3s, 238 sentence MP3s, all rendered via Google Cloud Wavenet voices, two-voice pair (Wavenet-D female, Wavenet-B male) deterministically assigned by lemma hash.

## Implementation roadmap

The work breaks into three tiers. Tier 1 is content extension, tier 2 is small features that fall out of usage, tier 3 is structural changes that need a design decision first.

### Tier 1, content extension

This is where most contributions land. Pure additions, no risk.

1. Seed ranks 201 to 400 with sentences. Each lemma needs an `english`, a primary sentence with `literal_gloss`, six variations. Roughly 200 lemmas, plan for two to three sittings.
2. Render audio for the new lemmas (`python app/tts_audio.py --scope words` after reseeding) and the new sentences (`--scope sentences`).
3. Same loop for ranks 401 to 600, 601 to 800, 801 to 1000.

Done condition for the project: ranks 1 to 1000 fully seeded. Past 1000, the marginal lemma is too low-frequency to deserve daily review at A1-A2, the deck retires and the user moves on to whatever they are reading and writing.

### Tier 2, small features

These are not committed to a release schedule. They land when the bottleneck shifts to them.

- Sentence-level audio in the New words view. Currently the play button shows for the headword only, the sentence MP3s are wired through the DB but not surfaced in this tab.
- Per-day new-card cap configurable from the UI rather than the CLI flag.
- Export-to-Anki. A small script that walks `seed_data.py` plus the user's `reviews` table and writes a `.apkg` file. Useful for users who want their progress portable across devices.
- A simple search box in the frontend, "find a card by lemma".

### Tier 3, structural changes

These need an issue and discussion before code lands.

- FSRS as an opt-in second scheduler. The grade signal is identical so the scheduler is replaceable, but the storage shape and the migration policy need design work.
- Multi-deck support. Currently one user, one deck. A second deck (other languages, other registers) would need namespacing in the DB and a deck picker in the UI.
- Server-side persistence for cross-device sync. Doable but moves the project from "local-first single-user" to "needs a backend", which is a different project. Probably out of scope.

## Release cadence

There are no releases. The repo is a deck-and-app pair, not a packaged tool. `git pull` is the upgrade path.

If a change is breaking (rare, would need a non-additive schema migration), I will tag a commit `pre-<change-name>` so users can roll back.

## Issue triage

Three labels.

- `content`: a sentence is wrong, a gloss is missing, an audio file is mispronounced. Resolved by editing `seed_data.py` or rerendering audio.
- `bug`: the code does the wrong thing. Should come with a reproducer (`./dutch stats` output, browser console error, OS, Python version).
- `enhancement`: a feature request. Will be discussed against the tier system above.

I aim to acknowledge issues within a week. Resolution is best-effort, this is a personal project that I share, not a service I run.

## How the deck grows over time

The deck is `data/words.csv` for the catalog and `app/seed_data.py` for the cards. Two distinct files because they have different lifecycles.

`words.csv` is rebuilt only when the upstream OpenSubtitles list updates. `build_wordlist.py` re-runs the lemmatization, the file is regenerated end-to-end. This happens roughly once a year, more often if there is a meaningful corpus update upstream.

`seed_data.py` grows whenever I (or a contributor) authors a new sentence. The file is the single source of truth for the deck, it is not generated. The seeder upserts everything, so reseeding is safe.

When a wave of new sentences lands, the audio renderer should be run to fill in MP3s. This is gated on a Google Cloud key, not everyone has one, the convention is that I render audio for any new wave I merge so the public deck stays complete. If you contribute sentences without audio, I will render the audio when I merge, no need to set up GCP yourself.

## Decisions, recorded

A short log so future-me does not relitigate calls already made.

**2026-04, SM-2 over FSRS.** Picked SM-2 because it is small, deterministic, and easy to debug from the review log. FSRS needs a few hundred personal reviews before it tunes. The grade signal is identical so we can swap later. Status: stable, no plans to revisit before the deck reaches ranks 1 to 500 fully seeded.

**2026-04, SQLite outside the project tree.** The project lived in an iCloud-synced folder originally, SQLite WAL files corrupted under sync. Moved DB to user-data dir per OS, kept human-readable assets in the repo. Status: still correct, do not undo.

**2026-04, one canonical sentence per sense.** The card front shows one sentence at a time so recall is anchored to one usage. The back lists every sense. Considered showing all senses on the front to expose breadth on every review, decided against because it dilutes the signal. Revisit if review quality on polysemes (`er`, `nog`) drops noticeably.

**2026-05, six-variation framework on a fixed axis set.** `past`, `future`, `question`, `verb`, `noun`, `person`. Considered allowing arbitrary variation tags, decided against because the pill UI is cleaner and the constraint forces sentence authors to think about each axis. The two tense pills are picked from the original's tense (a present-tense original yields `past` and `future`).

**2026-05, literal gloss must be a permutation, not a paraphrase.** The Word order drill compares submitted token order to the canonical Dutch tokenization. Allowing the gloss to add or substitute words would mean teaching token mismatches as legal answers. The constraint also forces sentence authors to write structurally drillable Dutch, which is itself useful pedagogically.

**2026-05, append-only logs.** Vocab Log, Feedback Log, Daily summaries are all grow-only. The chronological history is the data, rewriting it loses signal. If a row is wrong, append a correction, do not edit in place.

## Security and abuse

- The Flask server is a single-process, single-user dev server. Do not deploy publicly. There is no auth.
- The project does not collect telemetry, analytics, or any data that leaves the user's machine, except when `DUTCH_SRS_VAULT` is set, in which case the user has explicitly opted into local file writes.
- The optional Google Cloud TTS integration ships service-account JSON paths via env var. The `.gitignore` excludes `*.json` at the root by default. If you author a new tool that needs a credential, follow the same pattern.

## Backups

Backing up is your responsibility.

- The repo is fine, push to GitHub, that is the backup.
- The SQLite DB, that is the part that holds your progress. macOS Time Machine covers it. On Linux, add `~/.local/share/dutch-srs/` to your usual backup set. On Windows, `%LOCALAPPDATA%\dutch-srs\` is what you want.
- A periodic export-to-Anki feature would make this easier, see Tier 2 above.

## Stewardship and bus factor

I am one person. If I stop working on this, fork it, the code is small (under 5000 lines of Python plus a single-page HTML), the dependencies are minimal (Flask), and the data is plain text plus MP3. The MIT license lets you do anything you want with it.

If you maintain a meaningful fork, I am happy to link to it from this README. Open an issue with `discussion` in the title.
