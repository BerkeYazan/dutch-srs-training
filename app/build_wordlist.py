"""Build a cleaned top 1000 Dutch lemma list from the OpenSubtitles frequency dump.

Source: Hermit Dave's FrequencyWords repo, nl_50k.txt (subtitle-derived counts).
Reasoning: subtitles approximate spoken, A1-A2 register, which is the band a
beginner-to-lower-intermediate learner actually meets in conversation.

Pipeline:
1. Read raw surface forms with counts.
2. Drop noise: punctuation tokens, single chars, English bleed-through, numbers.
3. Lemmatize each surface form via spaCy nl_core_news_sm.
4. Aggregate counts onto lemmas (so ben/is/was all roll up onto zijn).
5. Drop closed-class noise we do not want to drill as flashcards (single letters,
   stray punctuation), but keep pronouns, articles, and prepositions, they are
   the workhorse vocabulary.
6. Keep top 1000 lemmas by aggregated count.

Output: SRS/data/words.csv with columns rank, lemma, pos, freq.

This script writes only the lemma list. English glosses and example sentences
are filled in by a separate authoring pass, see build_seed.py.

Reference: see Dutch Learning/SRS/README.md for the full design.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

import spacy

REPO = Path(__file__).resolve().parents[1]
RAW = Path("/tmp/nl_50k.txt")
OUT = REPO / "data" / "words.csv"

# Tokens that are clearly junk after splitting on whitespace.
NOISE = re.compile(r"^[^a-zA-ZàáâäèéêëìíîïòóôöùúûüçñÀÁÂÄÈÉÊËÌÍÎÏÒÓÔÖÙÚÛÜÇÑ]+$")

# Spurious entries that appear in subtitle dumps, English leakage etc.
BLOCKLIST = {
    "yeah", "okay", "ok", "uh", "hi", "hey", "ah", "oh", "uhm", "mmm",
    "well", "yes", "no",
}


def looks_dutch(token: str) -> bool:
    if len(token) < 2:
        # keep 'u' (formal you) and 'a' would be Italian, drop everything else len 1
        return token == "u"
    if NOISE.match(token):
        return False
    if token in BLOCKLIST:
        return False
    if any(ch.isdigit() for ch in token):
        return False
    return True


def main() -> None:
    nlp = spacy.load("nl_core_news_sm", disable=["ner", "parser"])

    # Read top N raw forms, more than 1000 because lemmatization will collapse rows.
    raw_rows: list[tuple[str, int]] = []
    with RAW.open() as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            tok, count = parts[0].lower(), int(parts[1])
            if not looks_dutch(tok):
                continue
            raw_rows.append((tok, count))
            if len(raw_rows) >= 5000:
                break

    # Lemmatize. We process tokens one at a time to keep token-to-lemma mapping
    # stable. spaCy on a single token returns a single Doc.
    agg: dict[tuple[str, str], int] = defaultdict(int)
    for tok, count in raw_rows:
        doc = nlp(tok)
        if not len(doc):
            continue
        t = doc[0]
        lemma = t.lemma_.lower()
        pos = t.pos_
        if not lemma or NOISE.match(lemma):
            continue
        agg[(lemma, pos)] += count

    # Some lemmas will appear under multiple POS, eg een (NUM, DET). Collapse onto
    # the highest-count POS for each lemma to keep one row per lemma.
    by_lemma: dict[str, tuple[str, int]] = {}
    for (lemma, pos), c in agg.items():
        cur = by_lemma.get(lemma)
        if cur is None or c > cur[1]:
            by_lemma[lemma] = (pos, c)

    ranked = sorted(by_lemma.items(), key=lambda kv: -kv[1][1])[:1000]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "lemma", "pos", "freq"])
        for i, (lemma, (pos, c)) in enumerate(ranked, start=1):
            w.writerow([i, lemma, pos, c])

    print(f"wrote {OUT} with {len(ranked)} rows")


if __name__ == "__main__":
    main()
