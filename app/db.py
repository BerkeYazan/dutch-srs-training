"""SQLite persistence layer for the Dutch SRS app.

The schema lives in schema.sql next to this file. The functions here are thin
wrappers over sqlite3, no ORM, the schema is small and the queries are short.
See srs.py for the algorithm and SRS/README.md for design.

Database location: the SQLite database lives outside the project folder so
that cloud-synced project trees (iCloud, Dropbox) do not corrupt SQLite WAL
files. On macOS the default path is ~/Library/Application Support/dutch-srs/srs.db,
on Linux ~/.local/share/dutch-srs/srs.db, on Windows %LOCALAPPDATA%\dutch-srs\srs.db.
Override with the env var DUTCH_SRS_DB to relocate. Human-readable assets like
words.csv stay in the project folder, only the SRS binary state moves out.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from srs import Card, State

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _default_db_path() -> Path:
    """Return the OS-appropriate user-data location for srs.db.

    Resolution order:
    1. DUTCH_SRS_DB env var, if set, used verbatim.
    2. macOS: ~/Library/Application Support/dutch-srs/srs.db
    3. Windows: %LOCALAPPDATA%\\dutch-srs\\srs.db, falls back to %APPDATA%
    4. Linux/BSD: $XDG_DATA_HOME/dutch-srs/srs.db, falls back to
       ~/.local/share/dutch-srs/srs.db
    5. /tmp fallback for sandboxes and CI where the user-data dir is not
       writable.

    The DB is intentionally outside the project folder so cloud-sync setups
    (iCloud, Dropbox, OneDrive) do not corrupt SQLite WAL files.
    """
    override = os.environ.get("DUTCH_SRS_DB")
    if override:
        return Path(override).expanduser()

    home = Path.home()
    if sys.platform == "darwin":
        primary = home / "Library" / "Application Support" / "dutch-srs" / "srs.db"
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(home)
        primary = Path(base) / "dutch-srs" / "srs.db"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else home / ".local" / "share"
        primary = base / "dutch-srs" / "srs.db"

    candidates = [primary, Path("/tmp/dutch-srs/srs.db")]
    for p in candidates:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # Probe writability with a sentinel file.
            sentinel = p.parent / ".writable_probe"
            sentinel.write_text("ok")
            sentinel.unlink()
            return p
        except OSError:
            continue
    return candidates[-1]


DB_PATH = _default_db_path()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if missing. Idempotent.

    Also runs forward-only column migrations so older databases gain new
    columns without losing data. Current migrations: prev_card_json on
    review_log for full undo state, the sentence_variations table for the
    near-variation reveal in the Sentence forming view, tense plus form
    columns on sentences for the six-variation framework, and literal_gloss
    on sentences for the Word order drill, see Methodology.md section 11.
    """
    sql = SCHEMA_PATH.read_text()
    conn.executescript(sql)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(review_log)").fetchall()}
    if "prev_card_json" not in cols:
        conn.execute("ALTER TABLE review_log ADD COLUMN prev_card_json TEXT")
    sent_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sentences)").fetchall()}
    if "tense" not in sent_cols:
        conn.execute("ALTER TABLE sentences ADD COLUMN tense TEXT")
    if "form" not in sent_cols:
        conn.execute("ALTER TABLE sentences ADD COLUMN form TEXT")
    if "literal_gloss" not in sent_cols:
        # Forward-only migration. Existing rows land with NULL gloss, the
        # Word order drill skips them until build_glosses.py fills the column.
        conn.execute("ALTER TABLE sentences ADD COLUMN literal_gloss TEXT")
    word_cols = {r["name"] for r in conn.execute("PRAGMA table_info(words)").fetchall()}
    if "audio_path" not in word_cols:
        # Forward-only migration. Existing rows land with NULL audio, the
        # play button in the UI hides until tts_audio.py generates the MP3
        # and writes the path back here.
        conn.execute("ALTER TABLE words ADD COLUMN audio_path TEXT")
    if "audio_voice" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN audio_voice TEXT")
    sent_cols2 = {r["name"] for r in conn.execute("PRAGMA table_info(sentences)").fetchall()}
    if "audio_path" not in sent_cols2:
        # Forward-only migration. The sentence audio button hides for
        # rows where this stays null until tts_audio.py renders the clip.
        conn.execute("ALTER TABLE sentences ADD COLUMN audio_path TEXT")
    if "audio_voice" not in sent_cols2:
        conn.execute("ALTER TABLE sentences ADD COLUMN audio_voice TEXT")
    conn.commit()


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def upsert_word(
    conn: sqlite3.Connection,
    *,
    rank: int,
    lemma: str,
    pos: str | None,
    article: str | None,
    english: str,
    notes: str | None,
    added_on: str,
) -> int:
    """Insert or update a word. Returns the row id."""
    cur = conn.execute(
        """
        INSERT INTO words (rank, lemma, pos, article, english, notes, added_on)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lemma) DO UPDATE SET
            rank=excluded.rank,
            pos=excluded.pos,
            article=excluded.article,
            english=excluded.english,
            notes=excluded.notes
        RETURNING id
        """,
        (rank, lemma, pos, article, english, notes, added_on),
    )
    row = cur.fetchone()
    return int(row["id"])


def replace_sentences(
    conn: sqlite3.Connection, word_id: int, sentences: list[dict]
) -> None:
    """Replace the full set of sentences for a word, including their variations.

    Each sentence dict has keys: nl, en, sense (optional), level (optional),
    variations (optional list). The first sentence becomes the primary one
    shown on the card front. The rest appear on the card back as additional
    senses. Variations attach to their parent sentence via a foreign key in
    sentence_variations and only surface in the Sentence forming view, see
    Methodology.md, Generalization through near-variations.

    Each variation dict has keys: nl, en, varied. The varied field is a short
    label, typically one of noun, time, person, verb, that names the element
    that was swapped relative to the parent sentence.

    Reseeding is idempotent: existing sentence rows are deleted, which
    cascades to their variations, then the new set is inserted with explicit
    sort_order.
    """
    conn.execute("DELETE FROM sentences WHERE word_id = ?", (word_id,))
    for i, s in enumerate(sentences):
        cur = conn.execute(
            """
            INSERT INTO sentences
                (word_id, dutch, english, sense, level, tense, form,
                 is_primary, sort_order, literal_gloss)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            RETURNING id
            """,
            (
                word_id,
                s["nl"],
                s["en"],
                s.get("sense"),
                s.get("level", "A1"),
                s.get("tense"),
                s.get("form"),
                1 if i == 0 else 0,
                i,
                # literal_gloss is optional at insert time. It is normally
                # filled by app/build_glosses.py after the row exists. Custom
                # sentences submitted from the UI also land with NULL here
                # and get glossed on the next build pass.
                s.get("literal_gloss"),
            ),
        )
        sent_id = int(cur.fetchone()["id"])
        for j, v in enumerate(s.get("variations") or []):
            conn.execute(
                """
                INSERT INTO sentence_variations
                    (sentence_id, dutch, english, varied, sort_order)
                VALUES (?,?,?,?,?)
                """,
                (
                    sent_id,
                    v["nl"],
                    v["en"],
                    v.get("varied"),
                    j,
                ),
            )


def fetch_sentences(conn: sqlite3.Connection, word_id: int) -> list[dict]:
    """Return every sentence for a word with its variations attached.

    Each returned dict carries the sentence row's columns plus a sentence_id
    and a variations list. Callers that only render the canonical sentence
    can ignore the variations field, which is what the New words review flow
    does. The Sentence forming view consumes variations to reveal them one
    by one after the user uncovers the original Dutch.
    """
    cur = conn.execute(
        "SELECT id, dutch, english, sense, level, tense, form, "
        "is_primary, sort_order, literal_gloss, audio_path "
        "FROM sentences WHERE word_id = ? ORDER BY sort_order ASC",
        (word_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    for row in rows:
        sent_id = int(row.pop("id"))
        row["sentence_id"] = sent_id
        row["variations"] = fetch_variations(conn, sent_id)
    return rows


def fetch_variations(conn: sqlite3.Connection, sentence_id: int) -> list[dict]:
    """Return the ordered list of near-variations for a single sentence."""
    cur = conn.execute(
        "SELECT dutch, english, varied, sort_order "
        "FROM sentence_variations WHERE sentence_id = ? ORDER BY sort_order ASC",
        (sentence_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def get_or_create_review(conn: sqlite3.Connection, word_id: int, due_at: datetime) -> Card:
    """Read the SRS state for a word, creating a 'new' row if missing."""
    cur = conn.execute("SELECT * FROM reviews WHERE word_id = ?", (word_id,))
    row = cur.fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO reviews (word_id, state, step, ease, interval_days, repetitions,
                                 lapses, due_at, last_reviewed)
            VALUES (?, 'new', 0, 2.5, 0, 0, 0, ?, NULL)
            """,
            (word_id, iso(due_at)),
        )
        conn.commit()
        cur = conn.execute("SELECT * FROM reviews WHERE word_id = ?", (word_id,))
        row = cur.fetchone()
    return _row_to_card(row)


def save_review(conn: sqlite3.Connection, word_id: int, card: Card) -> None:
    conn.execute(
        """
        UPDATE reviews
        SET state=?, step=?, ease=?, interval_days=?, repetitions=?, lapses=?,
            due_at=?, last_reviewed=?
        WHERE word_id=?
        """,
        (
            card.state,
            card.step,
            card.ease,
            card.interval_days,
            card.repetitions,
            card.lapses,
            iso(card.due_at),
            iso(card.last_reviewed) if card.last_reviewed else None,
            word_id,
        ),
    )


def log_review(
    conn: sqlite3.Connection,
    *,
    word_id: int,
    when: datetime,
    grade: int,
    prev: Card,
    new: Card,
) -> None:
    conn.execute(
        """
        INSERT INTO review_log
            (word_id, reviewed_at, grade, prev_state, new_state,
             prev_interval, new_interval, prev_ease, new_ease, prev_card_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            word_id,
            iso(when),
            grade,
            prev.state,
            new.state,
            prev.interval_days,
            new.interval_days,
            prev.ease,
            new.ease,
            _card_to_json(prev),
        ),
    )


def _card_to_json(c: Card) -> str:
    """Serialize a Card to JSON for the undo log.

    Datetimes need explicit ISO conversion since asdict does not handle them.
    """
    d = asdict(c)
    d["due_at"] = iso(c.due_at) if c.due_at else None
    d["last_reviewed"] = iso(c.last_reviewed) if c.last_reviewed else None
    return json.dumps(d)


def _card_from_json(s: str) -> Card:
    d = json.loads(s)
    return Card(
        state=d["state"],
        step=d["step"],
        ease=d["ease"],
        interval_days=d["interval_days"],
        repetitions=d["repetitions"],
        lapses=d["lapses"],
        due_at=parse_iso(d["due_at"]) if d.get("due_at") else datetime.now(timezone.utc),
        last_reviewed=parse_iso(d["last_reviewed"]) if d.get("last_reviewed") else None,
    )


def undo_last_review(conn: sqlite3.Connection) -> dict | None:
    """Pop the most recent review_log entry and restore the reviews row.

    Returns metadata about the undone event so the caller can adjust session
    counters and re-display the card. Returns None if there is no history.

    The restored card's full state comes from prev_card_json. For old log rows
    without that column populated, we reconstruct a minimal Card using the
    fields the legacy schema captured.
    """
    row = conn.execute(
        """
        SELECT id, word_id, grade, prev_state, prev_interval, prev_ease,
               prev_card_json, reviewed_at
        FROM review_log
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None

    if row["prev_card_json"]:
        prev = _card_from_json(row["prev_card_json"])
    else:
        # Legacy row, best-effort restore.
        prev = Card(
            state=row["prev_state"],
            step=0,
            ease=row["prev_ease"],
            interval_days=row["prev_interval"],
            repetitions=0,
            lapses=0,
            due_at=datetime.now(timezone.utc),
            last_reviewed=None,
        )

    save_review(conn, int(row["word_id"]), prev)
    conn.execute("DELETE FROM review_log WHERE id = ?", (row["id"],))
    return {
        "word_id": int(row["word_id"]),
        "was_new": row["prev_state"] == "new",
        "prev_state": row["prev_state"],
        "grade": int(row["grade"]),
        "reviewed_at": row["reviewed_at"],
    }


def history_count(conn: sqlite3.Connection, since: str | None = None) -> int:
    """Return how many review_log rows exist, optionally since an ISO time.

    Used by the frontend to decide whether the undo button should be active.
    """
    if since is None:
        return int(conn.execute("SELECT COUNT(*) c FROM review_log").fetchone()["c"])
    return int(
        conn.execute(
            "SELECT COUNT(*) c FROM review_log WHERE reviewed_at >= ?", (since,)
        ).fetchone()["c"]
    )


def due_word_ids(
    conn: sqlite3.Connection,
    now: datetime,
    limit: int,
    states: tuple[str, ...] = ("learning", "review", "relearning"),
) -> list[int]:
    """Return word ids whose review is due, oldest-due first.

    Excludes 'new' state by default, those are pulled separately to enforce a
    daily cap. The `states` filter lets the web queue request a subset, eg
    only ('learning', 'relearning') for the always-on short steps or only
    ('review',) for the daily-capped stream. See web.api_next.
    """
    placeholders = ",".join("?" for _ in states)
    cur = conn.execute(
        f"""
        SELECT word_id FROM reviews
        WHERE state IN ({placeholders})
          AND due_at <= ?
        ORDER BY due_at ASC
        LIMIT ?
        """,
        (*states, iso(now), limit),
    )
    return [int(r["word_id"]) for r in cur.fetchall()]


def due_count(
    conn: sqlite3.Connection,
    now: datetime,
    states: tuple[str, ...] = ("review",),
) -> int:
    """Count cards due now in the given states. Used to size the daily queue
    without materializing the full id list when only the total matters.
    """
    placeholders = ",".join("?" for _ in states)
    return int(
        conn.execute(
            f"SELECT COUNT(*) c FROM reviews WHERE state IN ({placeholders}) AND due_at <= ?",
            (*states, iso(now)),
        ).fetchone()["c"]
    )


def count_new(conn: sqlite3.Connection) -> int:
    """Count words still in the 'new' state, ie the size of the new pool."""
    return int(
        conn.execute("SELECT COUNT(*) c FROM reviews WHERE state = 'new'").fetchone()["c"]
    )


def new_word_ids(conn: sqlite3.Connection, limit: int) -> list[int]:
    """Return up to `limit` words still in the 'new' state.

    Custom-added words (assigned rank 0 by api_custom_sentence) sort
    ahead of seed words (rank >= 1), and within the custom bucket the
    most recently inserted comes first. The user just typed it in,
    they expect the next card to be that one. Beyond the custom
    bucket, seed words come back in frequency-rank order.
    """
    cur = conn.execute(
        """
        SELECT r.word_id
        FROM reviews r JOIN words w ON w.id = r.word_id
        WHERE r.state = 'new'
        ORDER BY w.rank ASC, w.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [int(r["word_id"]) for r in cur.fetchall()]


def set_sentence_audio(
    conn: sqlite3.Connection,
    *,
    sentence_id: int,
    audio_path: str,
    audio_voice: str,
) -> None:
    """Record a generated audio clip for a sentence.

    Mirrors set_word_audio. Keyed by sentence_id rather than the Dutch
    text so the row survives content edits, the audio_path uses the same
    id so the file on disk is stable too. tts_audio.py is the only
    caller today.
    """
    conn.execute(
        "UPDATE sentences SET audio_path = ?, audio_voice = ? WHERE id = ?",
        (audio_path, audio_voice, sentence_id),
    )
    conn.commit()


def set_word_audio(
    conn: sqlite3.Connection,
    *,
    lemma: str,
    audio_path: str,
    audio_voice: str,
) -> None:
    """Record a generated audio clip for a lemma.

    Called by app/tts_audio.py after a Google Cloud TTS render lands on disk.
    The audio_path is the static-relative URL the Flask app serves, not an
    absolute filesystem path, so the same row works for both the local app
    and any future redeploy that keeps the static layout.
    """
    conn.execute(
        "UPDATE words SET audio_path = ?, audio_voice = ? WHERE lemma = ?",
        (audio_path, audio_voice, lemma),
    )
    conn.commit()


def fetch_word(conn: sqlite3.Connection, word_id: int) -> dict:
    """Return a word row enriched with its primary sentence and a list of all
    sentences. Caller uses 'sentences' to render the card back when multiple
    senses exist.
    """
    cur = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,))
    row = cur.fetchone()
    if not row:
        return {}
    out = dict(row)
    out["sentences"] = fetch_sentences(conn, word_id)
    primary = next((s for s in out["sentences"] if s["is_primary"]), None)
    out["sent_dutch"] = primary["dutch"] if primary else ""
    out["sent_english"] = primary["english"] if primary else ""
    out["sent_sense"] = primary["sense"] if primary else None
    return out


def stats(conn: sqlite3.Connection) -> dict:
    out = {}
    out["total_words"] = conn.execute("SELECT COUNT(*) c FROM words").fetchone()["c"]
    by_state = conn.execute(
        "SELECT state, COUNT(*) c FROM reviews GROUP BY state"
    ).fetchall()
    out["by_state"] = {r["state"]: r["c"] for r in by_state}
    out["due_now"] = conn.execute(
        "SELECT COUNT(*) c FROM reviews WHERE state IN ('learning','review','relearning') AND due_at <= ?",
        (iso(datetime.now(timezone.utc)),),
    ).fetchone()["c"]
    return out


def _row_to_card(row: sqlite3.Row) -> Card:
    return Card(
        state=row["state"],
        step=row["step"],
        ease=row["ease"],
        interval_days=row["interval_days"],
        repetitions=row["repetitions"],
        lapses=row["lapses"],
        due_at=parse_iso(row["due_at"]),
        last_reviewed=parse_iso(row["last_reviewed"]) if row["last_reviewed"] else None,
    )


# ---------- Eligibility and Word-order helpers ----------
#
# The Sentence forming and Word order drills share one eligibility gate, see
# Methodology.md section 11. A sentence is eligible as soon as its headword
# has been started, ie state in (learning, review, relearning). The 'new'
# state is excluded because the word has not been seen yet. Tightening the
# gate further to graduated-only was tried earlier and made the queues too
# sparse, the user wants both production drills available throughout the
# learning curve.


def eligible_sentences_for_drill(
    conn: sqlite3.Connection,
    *,
    require_gloss: bool = False,
) -> list[dict]:
    """Return sentences whose headword has been started, joined with SRS state.

    The returned dicts carry the columns the production endpoints need to
    sample and render: sentence_id, word_id, lemma, article, pos, sense,
    dutch, english, tense, form, level, literal_gloss, plus the SRS state,
    lapses, and ease used for difficulty weighting.

    Eligibility window: state in ('learning', 'review', 'relearning'). A
    word in 'new' state has not yet been shown to the user, including its
    sentences in a production drill would surface unfamiliar lemmas.

    Args:
        conn: open SQLite connection.
        require_gloss: if True, restrict to sentences with a non-null
            literal_gloss. Word order needs this, Sentence forming does not.

    Returns:
        A flat list of row dicts, no nesting. Variations are intentionally
        not joined here, the Sentence forming endpoint fetches them per
        sentence so the Word order endpoint does not pay the cost.
    """
    # word_added_on (from words.added_on) is included so the queue endpoints
    # can apply a recency boost to sentences whose headword entered the deck
    # recently. Without this term the sample is dominated by the long-learned
    # core and freshly-added words rarely come up. See web._recency_boost.
    base = (
        """
        SELECT s.id AS sentence_id, s.word_id, s.dutch, s.english, s.sense,
               s.tense, s.form, s.level, s.literal_gloss,
               s.audio_path AS sentence_audio_path,
               w.lemma, w.article, w.pos, w.english AS word_english,
               w.audio_path AS word_audio_path, w.added_on AS word_added_on,
               r.state, r.lapses, r.ease
        FROM sentences s
        JOIN words w   ON w.id = s.word_id
        JOIN reviews r ON r.word_id = s.word_id
        WHERE r.state IN ('learning', 'review', 'relearning')
        """
    )
    if require_gloss:
        base += " AND s.literal_gloss IS NOT NULL AND s.literal_gloss <> ''"
    rows = conn.execute(base).fetchall()
    return [dict(r) for r in rows]


def sentences_missing_gloss(conn: sqlite3.Connection) -> list[dict]:
    """Return every sentence row whose literal_gloss is still NULL or empty.

    Drives app/build_glosses.py. Ordered by word frequency rank so the most
    useful sentences gloss first when the script runs partial.
    """
    rows = conn.execute(
        """
        SELECT s.id AS sentence_id, s.word_id, s.dutch, s.english, s.sense,
               s.tense, s.form, w.lemma, w.rank
        FROM sentences s
        JOIN words w ON w.id = s.word_id
        WHERE s.literal_gloss IS NULL OR s.literal_gloss = ''
        ORDER BY w.rank ASC, s.sort_order ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def set_literal_gloss(
    conn: sqlite3.Connection, sentence_id: int, gloss: str
) -> None:
    """Persist a literal gloss for a sentence. No validation here, the caller
    is responsible for checking the token multiset matches the canonical Dutch.
    """
    conn.execute(
        "UPDATE sentences SET literal_gloss = ? WHERE id = ?",
        (gloss, sentence_id),
    )


def log_word_order_attempt(
    conn: sqlite3.Connection,
    *,
    sentence_id: int,
    word_id: int,
    when: datetime,
    ok: bool,
    submitted_order: list[str],
    canonical_order: list[str],
    misplaced_count: int,
) -> None:
    """Append one row to word_order_attempts. See schema.sql for the column
    semantics, this is not on the SM-2 path, see Methodology.md section 11.
    """
    conn.execute(
        """
        INSERT INTO word_order_attempts
            (sentence_id, word_id, attempted_at, ok,
             submitted_order, canonical_order, misplaced_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sentence_id,
            word_id,
            iso(when),
            1 if ok else 0,
            json.dumps(submitted_order, ensure_ascii=False),
            json.dumps(canonical_order, ensure_ascii=False),
            int(misplaced_count),
        ),
    )


def sync_glosses_from_seed(
    conn: sqlite3.Connection, seed: dict
) -> dict:
    """Copy literal_gloss values from seed_data.SEED into the DB without
    deleting or replacing sentence rows.

    Why not just call replace_sentences again. Sentences have ON DELETE
    CASCADE for sentence_variations and for word_order_attempts, so wiping
    and reseeding would also drop variations and the entire attempts log
    every time the seed file gains a new gloss. This helper updates only
    the literal_gloss column, in place, on rows it can match by (lemma,
    dutch text). Custom sentences and any seed entries whose Dutch has
    drifted from the file are left alone.

    Returns a small report dict with counts the caller can print.
    """
    updated = 0
    skipped_drift = 0
    for lemma, entry in seed.items():
        wid_row = conn.execute(
            "SELECT id FROM words WHERE lemma = ?", (lemma,)
        ).fetchone()
        if wid_row is None:
            continue
        word_id = int(wid_row["id"])
        # Build the list of (canonical_dutch, gloss) pairs from the seed
        # entry. Both shapes are handled: 'sentences' list and the legacy
        # single-sentence keys.
        pairs: list[tuple[str, str]] = []
        if "sentences" in entry:
            for s in entry["sentences"]:
                gloss = s.get("literal_gloss")
                if gloss:
                    pairs.append((s["nl"], gloss))
        else:
            gloss = entry.get("literal_gloss")
            if gloss and entry.get("sentence_nl"):
                pairs.append((entry["sentence_nl"], gloss))
        for canon_nl, gloss in pairs:
            cur = conn.execute(
                """
                UPDATE sentences
                SET literal_gloss = ?
                WHERE word_id = ?
                  AND dutch = ?
                  AND (literal_gloss IS NULL OR literal_gloss = '' OR literal_gloss <> ?)
                """,
                (gloss, word_id, canon_nl, gloss),
            )
            if cur.rowcount > 0:
                updated += cur.rowcount
            else:
                # rowcount 0 means no row matched the (word_id, dutch)
                # tuple, which is the drift case. Either the seed sentence
                # was edited and the DB has the old text, or the DB does
                # not yet have this lemma's sentences at all. Either way,
                # we leave it alone, the seed step is the right tool to
                # add new sentence rows.
                exists = conn.execute(
                    "SELECT 1 FROM sentences WHERE word_id = ? AND dutch = ?",
                    (word_id, canon_nl),
                ).fetchone()
                if exists is None:
                    skipped_drift += 1
    conn.commit()
    return {"updated": updated, "skipped_drift": skipped_drift}


def word_order_failure_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """Return a {sentence_id: failure_count} map over word_order_attempts.

    Used by the Word order sampler to bias the queue toward sentences the user
    has gotten wrong before. Only failures are counted, successes are not
    discounted, the soft weight stays positive for all eligible sentences.
    """
    rows = conn.execute(
        """
        SELECT sentence_id, COUNT(*) AS c
        FROM word_order_attempts
        WHERE ok = 0
        GROUP BY sentence_id
        """
    ).fetchall()
    return {int(r["sentence_id"]): int(r["c"]) for r in rows}
