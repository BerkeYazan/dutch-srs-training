"""Command-line entry point for the Dutch SRS app.

Subcommands:
    seed     load words from words.csv plus seed_data.py into the database
    review   run a review session: due cards first, then up to N new
    stats    print deck size, due count, and per-state counts
    add      add a single word with sentence (interactive)

Design notes:
- Single-file CLI on argparse. No click, no rich. Avoids dependency creep.
- The interactive review loop is keystroke based: 1=again, 2=hard, 3=good, 4=easy,
  q=quit, s=skip, e=edit (opens editor on the example sentence).
- Each session appends new-word rows to Vocab Log via vault.py.

Usage:
    python -m app.cli seed
    python -m app.cli review --new 20
    python -m app.cli stats

Style: top-level docstring states purpose, comments explain why and not what,
non-obvious choices link out to README.md or MAINTENANCE.md.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `python app/cli.py` and `python -m app.cli` both to work.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db  # noqa: E402
import srs  # noqa: E402
from seed_data import SEED  # noqa: E402
from vault import append_to_vocab_log, append_to_daily, today_iso  # noqa: E402

ROOT = HERE.parent
WORDS_CSV = ROOT / "data" / "words.csv"
LOG_DIR = ROOT / "logs"

DAILY_NEW_DEFAULT = 20


def cmd_seed(args: argparse.Namespace) -> None:
    """Load words.csv and the SEED dict into the database.

    Only lemmas that appear in SEED get sentences. Lemmas without seed entries
    still go into the words table (so they can become cards once a sentence is
    added later) but they are NOT given a 'reviews' row, which means they sit
    outside the new queue until activated.

    Each SEED entry can carry either a single sentence (legacy keys
    sentence_nl/sentence_en) or a list under 'sentences', each item being a
    dict with nl, en, optional sense, optional level. Polysemous words like
    'nog' use the list form so the card back can show every sense.
    """
    conn = db.connect()
    db.init_db(conn)

    with WORDS_CSV.open() as f:
        reader = csv.DictReader(f)
        added = 0
        seeded = 0
        for row in reader:
            lemma = row["lemma"]
            entry = SEED.get(lemma)
            if entry is None:
                # Catalog the word but do not card it yet.
                db.upsert_word(
                    conn,
                    rank=int(row["rank"]),
                    lemma=lemma,
                    pos=row["pos"],
                    article=None,
                    english="",
                    notes=None,
                    added_on=today_iso(),
                )
                added += 1
                continue

            wid = db.upsert_word(
                conn,
                rank=int(row["rank"]),
                lemma=lemma,
                pos=row["pos"],
                article=entry.get("article"),
                english=entry["english"],
                notes=entry.get("notes"),
                added_on=today_iso(),
            )
            sentences = _normalize_sentences(entry)
            db.replace_sentences(conn, word_id=wid, sentences=sentences)
            db.get_or_create_review(conn, wid, due_at=datetime.now(timezone.utc))
            seeded += 1
            added += 1

    conn.commit()
    linked_words, linked_sents = _link_existing_audio(conn)
    if linked_words or linked_sents:
        print(f"linked existing audio: {linked_words} word clip(s), {linked_sents} sentence clip(s)")
    print(f"catalog has {added} words, {seeded} have sentence cards in the new queue")


def _link_existing_audio(conn) -> tuple[int, int]:
    """Wire up MP3 files that ship in the repo to the DB rows.

    A fresh clone has MP3s in app/static/audio/{words,sentences} but the
    audio_path columns are NULL until tts_audio.py runs. This helper closes
    that gap by scanning the static folders and writing the matching paths
    so the play button shows on first run, no GCP credential required.

    Idempotent: rows whose audio_path already matches the on-disk file are
    not rewritten, so reseeding does not churn the DB.
    """
    static = HERE / "static"
    words_dir = static / "audio" / "words"
    sents_dir = static / "audio" / "sentences"

    linked_words = 0
    if words_dir.exists():
        for mp3 in words_dir.glob("*.mp3"):
            lemma = mp3.stem
            rel = f"audio/words/{mp3.name}"
            cur = conn.execute(
                "SELECT audio_path FROM words WHERE lemma = ?", (lemma,)
            ).fetchone()
            if cur is None:
                continue
            if cur["audio_path"] == rel:
                continue
            conn.execute(
                "UPDATE words SET audio_path = ? WHERE lemma = ?", (rel, lemma)
            )
            linked_words += 1

    # Sentence audio is keyed by a stable hash of the Dutch text rather than
    # by SQLite's auto-increment id. The id changes between fresh seeds and
    # across machines, the Dutch text does not, so hashing on the canonical
    # string is the only key that lets pre-rendered MP3s ship in the repo
    # and link correctly anywhere. The hash function is sha1, truncated to
    # 16 hex chars: collision risk on a few thousand short sentences is
    # cosmologically small. See scripts/migrate_sentence_audio.py for the
    # one-shot rename that brought the existing files onto this scheme.
    linked_sents = 0
    if sents_dir.exists():
        rows = conn.execute("SELECT id, dutch FROM sentences").fetchall()
        for row in rows:
            h = sentence_audio_hash(row["dutch"])
            mp3 = sents_dir / f"{h}.mp3"
            if not mp3.exists():
                continue
            rel = f"audio/sentences/{mp3.name}"
            cur = conn.execute(
                "SELECT audio_path FROM sentences WHERE id = ?", (row["id"],)
            ).fetchone()
            if cur and cur["audio_path"] == rel:
                continue
            conn.execute(
                "UPDATE sentences SET audio_path = ? WHERE id = ?", (rel, row["id"])
            )
            linked_sents += 1

    conn.commit()
    return linked_words, linked_sents


def sentence_audio_hash(dutch: str) -> str:
    """Stable filename stem for a sentence's audio clip.

    Computed from the canonical Dutch text after whitespace normalization.
    Whitespace normalization matters because exporters and editors sometimes
    introduce trailing spaces or NFC/NFD inconsistencies that would change
    the hash without changing the spoken content. We strip and collapse
    runs of whitespace to a single space, lowercase nothing (Dutch
    orthography is case-meaningful), and hash the result. 16 hex chars of
    sha1 is plenty for a few thousand sentences.
    """
    import hashlib
    import re
    norm = re.sub(r"\s+", " ", dutch.strip())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _normalize_sentences(entry: dict) -> list[dict]:
    """Accept either the legacy single-sentence keys or a 'sentences' list.

    Returns a list of dicts with keys nl, en, sense, level, tense, form,
    literal_gloss, variations. The first dict is the primary sentence shown
    on the card front. Each sentence may carry a variations list of dicts
    with keys nl, en, varied, used by the Sentence forming view to reveal
    near-variations one by one after the user uncovers the original Dutch.
    See Methodology.md, section Generalization through near-variations.

    The literal_gloss field, if present, is the Dutch tokens written in
    English word order, used by the Word order drill, see Methodology.md
    section 11. It is authored alongside the sentence in seed_data.py, not
    generated at build time. Sentences without a gloss are simply skipped
    from the Word order queue until the gloss is added.
    """
    if "sentences" in entry:
        out = []
        for s in entry["sentences"]:
            out.append({
                "nl": s["nl"],
                "en": s["en"],
                "sense": s.get("sense"),
                "level": s.get("level", entry.get("level", "A1")),
                "tense": s.get("tense"),
                "form": s.get("form"),
                "literal_gloss": s.get("literal_gloss"),
                "variations": _normalize_variations(s.get("variations")),
            })
        return out
    return [{
        "nl": entry["sentence_nl"],
        "en": entry["sentence_en"],
        "sense": entry.get("sense"),
        "level": entry.get("level", "A1"),
        "tense": entry.get("tense"),
        "form": entry.get("form"),
        "literal_gloss": entry.get("literal_gloss"),
        "variations": _normalize_variations(entry.get("variations")),
    }]


def _normalize_variations(variations: list[dict] | None) -> list[dict]:
    """Coerce raw seed variation dicts into the canonical persistence shape.

    Returns an empty list when no variations were provided, so downstream code
    can iterate without a None guard.
    """
    if not variations:
        return []
    out = []
    for v in variations:
        out.append({
            "nl": v["nl"],
            "en": v["en"],
            "varied": v.get("varied"),
        })
    return out


def _format_card(word_row: dict) -> tuple[str, str]:
    """Return (front, back) text for a card.

    Front shows the headword and the primary Dutch sentence. Back shows the
    English gloss, every attached sentence with its sense label, and any
    pedagogy notes. Multi-sense words display all senses on the back so
    recall is anchored to the full meaning landscape, not just one example.
    """
    lemma = word_row["lemma"]
    eng = word_row.get("english") or ""
    article = word_row.get("article")
    pos = word_row.get("pos") or ""
    head = f"{article} {lemma}" if article else lemma
    sentences = word_row.get("sentences") or []
    notes = word_row.get("notes") or ""

    primary = sentences[0] if sentences else None
    front_sent = primary["dutch"] if primary else ""
    front = f"\n  {head}    [{pos}]\n\n  {front_sent}\n"

    back_lines = [f"  {head}  =  {eng}", ""]
    for i, s in enumerate(sentences, start=1):
        label = f"({s['sense']}) " if s.get("sense") else ""
        back_lines.append(f"  {i}. {label}{s['dutch']}")
        back_lines.append(f"     -> {s['english']}")
    if notes:
        back_lines += ["", f"  note: {notes}"]
    back = "\n".join(back_lines)
    return front, back


def _grade_prompt() -> int | None:
    """Read a single keystroke for the grade. Returns None on quit."""
    while True:
        s = input("  [1] again  [2] hard  [3] good  [4] easy  [q] quit > ").strip().lower()
        if s in {"q", "quit"}:
            return None
        if s in {"1", "2", "3", "4"}:
            return int(s)
        print("  unrecognized")


def cmd_review(args: argparse.Namespace) -> None:
    """Run a review session.

    Order: due learning/relearning cards first (most urgent), then due review
    cards, then up to args.new from the new queue. We mirror Anki's mix.

    On a fresh machine the database has no words yet, the local
    Application Support folder is per-machine. If the catalog is empty we
    auto-seed before reviewing so the first run on a new computer just works.
    """
    conn = db.connect()
    db.init_db(conn)
    n_words = conn.execute("SELECT COUNT(*) c FROM words").fetchone()["c"]
    if n_words == 0:
        print("first run on this machine, seeding the deck...")
        cmd_seed(args)
        conn = db.connect()
    # Same auto-sync as cmd_web, see comment there.
    report = db.sync_glosses_from_seed(conn, SEED)
    if report["updated"]:
        print(f"synced {report['updated']} literal_gloss value(s) from seed_data.py")
    now = datetime.now(timezone.utc)
    today = today_iso()

    due_ids = db.due_word_ids(conn, now=now, limit=10000)
    new_ids = db.new_word_ids(conn, limit=args.new)
    queue = due_ids + new_ids

    if not queue:
        print("nothing due, deck is clean")
        return

    print(f"\nDutch SRS, {today}")
    print(f"  due now: {len(due_ids)}    new today: {len(new_ids)}\n")

    seen_new_words: list[dict] = []  # for vocab log integration
    correct = 0
    wrong = 0

    for wid in queue:
        word = db.fetch_word(conn, wid)
        if not word:
            continue
        front, back = _format_card(word)
        print("=" * 60)
        print(front)
        input("  press enter to flip ")
        print(back)
        prev = db.get_or_create_review(conn, wid, due_at=now)
        was_new = prev.state == "new"
        grade = _grade_prompt()
        if grade is None:
            print("  saving and exiting")
            break
        new = srs.review(prev, grade, at=datetime.now(timezone.utc))
        db.save_review(conn, wid, new)
        db.log_review(conn, word_id=wid, when=datetime.now(timezone.utc), grade=grade,
                      prev=prev, new=new)
        conn.commit()
        if grade == 1:
            wrong += 1
        else:
            correct += 1
        if was_new:
            seen_new_words.append({
                "word": (word.get("article") + " " if word.get("article") else "") + word["lemma"],
                "english": word.get("english", ""),
                "example": word.get("sent_dutch", ""),
                "source": "SRS day 1 (top 1k)",
                "tag": "#srs #core1k",
                "notes": word.get("notes") or "",
            })

    total = correct + wrong
    pct = (100 * correct // total) if total else 0
    print(f"\nsession done: {total} cards, {pct}% pass rate")

    if seen_new_words:
        append_to_vocab_log(today=today, rows=seen_new_words)
        print(f"appended {len(seen_new_words)} new words to Vocab Log")
    summary = f"SRS: {total} cards, {pct}% pass, {len(seen_new_words)} new words"
    append_to_daily(today=today, summary=summary)


def cmd_stats(args: argparse.Namespace) -> None:
    conn = db.connect()
    db.init_db(conn)
    s = db.stats(conn)
    print(f"total words in catalog : {s['total_words']}")
    print(f"due now                 : {s['due_now']}")
    for k, v in sorted(s["by_state"].items()):
        print(f"  {k:11s}: {v}")


def cmd_web(args: argparse.Namespace) -> None:
    """Start the Flask web UI and open the browser to it.

    Auto-seeds on a fresh machine like cmd_review does, so the first run on a
    new computer does not greet the user with an empty deck. Also re-seeds
    when the sentence_variations table is empty on a populated deck, which is
    the state right after the schema migration adds the table but before the
    variations have been filled in. Re-seeding only touches the sentences and
    sentence_variations tables, the reviews state is preserved.
    """
    import webbrowser
    import threading

    conn = db.connect()
    db.init_db(conn)
    n_words = conn.execute("SELECT COUNT(*) c FROM words").fetchone()["c"]
    if n_words == 0:
        print("first run on this machine, seeding the deck...")
        cmd_seed(args)
    else:
        n_sents = conn.execute("SELECT COUNT(*) c FROM sentences").fetchone()["c"]
        n_vars = conn.execute("SELECT COUNT(*) c FROM sentence_variations").fetchone()["c"]
        if n_sents > 0 and n_vars == 0:
            print("sentence_variations is empty, reseeding to fill variations...")
            cmd_seed(args)

    # Sync any newly-authored literal_gloss values from seed_data.py into
    # the DB without re-replacing sentence rows. Keeps SRS state, variations,
    # and word_order_attempts intact, see db.sync_glosses_from_seed. This is
    # the path the user actually exercises, so changes to the seed flow
    # through silently on the next web boot, no manual step.
    fresh_conn = db.connect()
    report = db.sync_glosses_from_seed(fresh_conn, SEED)
    if report["updated"]:
        print(f"synced {report['updated']} literal_gloss value(s) from seed_data.py")

    import web as web_app  # imported lazily so the CLI can run without flask

    url = f"http://{args.host}:{args.port}/?new={args.new}"
    print(f"Dutch SRS web UI: {url}")
    print("press ctrl+c to stop the server")
    threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    web_app.run(host=args.host, port=args.port)


def cmd_add(args: argparse.Namespace) -> None:
    """Add a custom word card outside the top-1k seed list.

    Useful when you encounter a word in the wild (NiG chapter, Dagboek, etc.)
    and want it in the deck without editing seed_data.py.
    """
    conn = db.connect()
    db.init_db(conn)

    lemma = args.word
    english = args.english or input("english gloss: ").strip()
    article = args.article
    sent_nl = args.sentence or input("Dutch sentence: ").strip()
    sent_en = args.sentence_en or input("English sentence: ").strip()
    # The literal gloss is optional. When present it is the Dutch tokens of
    # sent_nl reordered into English word order, used by the Word order
    # drill, see [[Methodology]] section 11. We do not prompt for it
    # interactively, the --gloss flag is the explicit way to supply one.
    gloss = args.gloss
    notes = args.notes

    rank = 9999  # off-list, sort to the end of the new queue
    wid = db.upsert_word(
        conn, rank=rank, lemma=lemma, pos=None, article=article,
        english=english, notes=notes, added_on=today_iso(),
    )
    db.replace_sentences(conn, word_id=wid, sentences=[{
        "nl": sent_nl,
        "en": sent_en,
        "sense": None,
        "level": "A2",
        "literal_gloss": gloss,
    }])
    db.get_or_create_review(conn, wid, due_at=datetime.now(timezone.utc))
    conn.commit()
    print(f"added {lemma}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dutch-srs", description="Dutch SRS, top 1k vocab.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("seed", help="load words.csv and seed sentences into the DB")
    sp.set_defaults(func=cmd_seed)

    sp = sub.add_parser("review", help="run a review session")
    sp.add_argument("--new", type=int, default=DAILY_NEW_DEFAULT,
                    help=f"max new cards this session (default {DAILY_NEW_DEFAULT})")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("stats", help="print deck statistics")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("web", help="start the web UI on localhost")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=5051)
    sp.add_argument("--new", type=int, default=DAILY_NEW_DEFAULT,
                    help=f"daily new-card cap (default {DAILY_NEW_DEFAULT})")
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("add", help="add a single word card")
    sp.add_argument("word")
    sp.add_argument("--english")
    sp.add_argument("--article", choices=["de", "het"])
    sp.add_argument("--sentence", dest="sentence")
    sp.add_argument("--sentence-en", dest="sentence_en")
    sp.add_argument("--gloss", dest="gloss",
                    help="literal gloss, Dutch tokens in English word order, optional")
    sp.add_argument("--notes")
    sp.set_defaults(func=cmd_add)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
