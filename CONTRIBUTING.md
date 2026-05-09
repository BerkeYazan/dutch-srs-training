# Contributing

Thanks for considering a change. The project is small, the bar is low, and most of what is welcome is content (sentences, glosses, variations) rather than code. This file is the short version of how to land a change cleanly.

## Setup

Same steps as a fresh install. Python 3.10 or newer.

```bash
git clone https://github.com/<your-user>/dutch-srs-training.git
cd dutch-srs-training
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
./dutch web
```

That gives you a working app and the dev dependencies (pytest, spaCy stubs, Google TTS client) pre-installed. None of those are needed to run, only to extend.

## What to send a PR for

Welcome:

- A new sentence for an unseeded lemma in `seed_data.py`.
- A correction to an existing sentence (typo, awkward phrasing, wrong sense label).
- A new literal gloss for a sentence that does not have one yet.
- A clarifying docstring or comment.
- A unit test that catches a real edge case.
- A bug fix with a test that demonstrates the bug.

Probably welcome, ask in an issue first:

- Algorithm changes in `srs.py`.
- API changes in `web.py`.
- Schema changes in `schema.sql`.
- New views or tabs in the frontend.

Probably not welcome:

- Cosmetic frontend changes without a clear pedagogical reason.
- New runtime dependencies. Flask is the only one for a reason.
- A second language. Fork instead, the code is small.

## Authoring a sentence

The hard part of this project is good sentences. A few guidelines that come from experience.

- **A1 or A2 register.** Common verbs, common nouns, no idioms unless the idiom is the headword.
- **Headword in place.** The lemma should appear in the sentence in a clear, central position. Variations should keep it in place where possible.
- **One canonical English gloss.** Translate the meaning, not the structure. The literal gloss is for word order, not for translation.
- **Sense labels for polysemes.** If the headword has more than one A1-A2 sense, write one sentence per sense and label them with `sense`.
- **Variations cover six axes.** `past`, `future`, `question`, `verb`, `noun`, `person`. Two of the six are tense alternatives derived from the original's tense, the other four are form or lexical swaps. Each variation should change exactly one axis.

The literal gloss is a permutation of the canonical Dutch tokens, in English word order. Same words, possibly reordered. No additions, no substitutions, no inflection changes. The seeder, the CLI, and the Custom endpoint all enforce this. If you find a sentence where the gloss looks impossible (modal-verb-final, perfectum), the right move is usually to rewrite the sentence so its English shape is reachable through reordering.

## Authoring a literal gloss

Take the canonical Dutch sentence, write its tokens in English word order, that is the gloss. Concrete example:

```
nl:    Morgen ga ik naar de markt op de fiets.
gloss: Morgen ik ga naar de markt op de fiets
en:    Tomorrow I am going to the market on the bike.
```

The Dutch verb-second rule pushes `ga` ahead of `ik`. English wants `I go`, so the gloss puts `ik` before `ga`. Same tokens, different order. The drill is "given English word order, recover the canonical Dutch order".

If your sentence has subordinate-clause-final verbs (`omdat ik moe ben`), the gloss flips the verb to its English position (`omdat ik ben moe`). Same words, different order.

## Running tests

```bash
python3 app/test_srs.py
```

Direct invocation works because the test module imports from `srs` on the
relative sys.path, no package install required.

The suite is small and intentionally focuses on the algorithm. UI and DB are covered by manual smoke tests.

## Style

- No emojis. Anywhere.
- No emdashes in markdown or code comments. Use commas, full stops, or parentheses.
- Comments explain why, not what. The code already shows the what.
- Top-level docstring on every Python module. Public functions get docstrings with Args, Returns, Raises where applicable.
- Plain prose in markdown. No marketing language, no superlatives, no cheerleading.
- Lowercase Git commit subjects, imperative mood, under 60 characters. Body wraps at 72.

## Pull request checklist

Before opening a PR, please:

- [ ] Run `python3 app/test_srs.py` and confirm green.
- [ ] If you changed the schema, run `DUTCH_SRS_DB=/tmp/test.db ./dutch seed && ./dutch stats` against an empty DB.
- [ ] If you added a sentence with a literal gloss, verify it shows up in Word order after seeding.
- [ ] Update `AGENTS.md` if you changed something an LLM working on this code should know.
- [ ] Update this file or `MAINTENANCE.md` if you changed how contribution or release works.

A PR description that says "I added 30 new sentences for ranks 201 to 230" with the seeded ranks listed is plenty. No template required.
