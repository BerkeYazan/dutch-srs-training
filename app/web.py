"""Flask web UI for the Dutch SRS app.

Single-page frontend served at /, calling JSON endpoints to walk the queue:
    GET  /api/next?new=N    -> next card or {"done": true}
    POST /api/grade         -> {word_id, grade}, applies SM-2 update
    GET  /api/stats         -> deck stats
    GET  /api/session       -> session counters since server start

The queue is rebuilt on every /api/next call: due learning/relearning first,
then due review, then up to N from the new pool. The server tracks how many
new cards have already been served this session to enforce the daily cap
across multiple page loads.

This module is the runtime UI for daily review. The CLI in cli.py remains
the canonical surface for seed, stats, add. See SRS/README.md for the wider
design.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

import db  # noqa: E402
import srs  # noqa: E402
from vault import (  # noqa: E402
    append_to_vocab_log,
    append_to_daily,
    append_to_feedback_log,
    retract_from_vocab_log,
    today_iso,
)

STATIC_DIR = HERE / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")
# Disable Flask's default 12-hour static-file cache so edits to index.html
# show up on the next reload instead of after the browser cache expires.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


# Session state is kept in module-level globals because there is one user and
# one process. If the server restarts the counters reset, which is fine.
_session_today: str = today_iso()
_session_new_seen: int = 0
_session_correct: int = 0
_session_wrong: int = 0
_session_new_words_logged: list[dict] = []


def _reset_if_new_day() -> None:
    """Reset the per-day counters when the date rolls over."""
    global _session_today, _session_new_seen, _session_correct, _session_wrong
    global _session_new_words_logged
    today = today_iso()
    if today != _session_today:
        _session_today = today
        _session_new_seen = 0
        _session_correct = 0
        _session_wrong = 0
        _session_new_words_logged = []


def _card_payload(conn, word_id: int) -> dict:
    """Serialize a word and its sentences for the frontend.

    Returns the headword, POS, optional article, English gloss, ordered list of
    sentences with sense labels and translations, plus pedagogy notes.
    """
    w = db.fetch_word(conn, word_id)
    return {
        "word_id": word_id,
        "lemma": w["lemma"],
        "pos": w.get("pos"),
        "article": w.get("article"),
        "english": w.get("english") or "",
        "notes": w.get("notes") or "",
        "sentences": w.get("sentences") or [],
        "rank": w.get("rank"),
        # audio_path is a static-relative URL, populated by tts_audio.py.
        # Stays None for words that have not been generated yet, the
        # frontend uses the falsy value to hide the play button.
        "audio_path": w.get("audio_path"),
    }


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/next")
def api_next():
    """Return the next card or {"done": true}.

    Query params:
        new : daily new-card cap (default 20)
    """
    _reset_if_new_day()
    new_cap = int(request.args.get("new", 20))
    new_remaining = max(0, new_cap - _session_new_seen)

    conn = db.connect()
    db.init_db(conn)
    if conn.execute("SELECT COUNT(*) c FROM words").fetchone()["c"] == 0:
        return jsonify({"empty": True})

    now = datetime.now(timezone.utc)
    due = db.due_word_ids(conn, now=now, limit=10000)
    history = db.history_count(conn)
    if due:
        return jsonify({
            "card": _card_payload(conn, due[0]),
            "is_new": False,
            "queue": {"due_ahead": len(due) - 1, "new_remaining": new_remaining},
            "history": history,
        })

    if new_remaining > 0:
        new_ids = db.new_word_ids(conn, limit=1)
        if new_ids:
            return jsonify({
                "card": _card_payload(conn, new_ids[0]),
                "is_new": True,
                "queue": {"due_ahead": 0, "new_remaining": new_remaining - 1},
                "history": history,
            })

    return jsonify({"done": True, "history": history})


@app.route("/api/grade", methods=["POST"])
def api_grade():
    """Apply a grade and persist the SRS state update.

    Body: {"word_id": int, "grade": 1..4}
    """
    global _session_new_seen, _session_correct, _session_wrong
    global _session_new_words_logged

    data = request.get_json(force=True) or {}
    word_id = int(data["word_id"])
    grade = int(data["grade"])
    if grade not in (1, 2, 3, 4):
        return jsonify({"error": "grade must be 1..4"}), 400

    conn = db.connect()
    now = datetime.now(timezone.utc)
    prev = db.get_or_create_review(conn, word_id, due_at=now)
    was_new = prev.state == "new"
    new = srs.review(prev, grade, at=now)
    db.save_review(conn, word_id, new)
    db.log_review(conn, word_id=word_id, when=now, grade=grade, prev=prev, new=new)
    conn.commit()

    if grade == 1:
        _session_wrong += 1
    else:
        _session_correct += 1

    if was_new:
        _session_new_seen += 1
        w = db.fetch_word(conn, word_id)
        primary = (w.get("sentences") or [{}])[0]
        row = {
            "word_id": word_id,  # tracked so undo can pop the right row
            "word": (w.get("article") + " " if w.get("article") else "") + w["lemma"],
            "english": w.get("english", ""),
            "example": primary.get("dutch", ""),
            "source": "SRS day 1 (top 1k)",
            "tag": "#srs #core1k",
            "notes": w.get("notes") or "",
        }
        # Write to Vocab Log immediately so a mid-session quit is non-lossy.
        # The buffer is kept for the daily-note summary on /api/finish.
        append_to_vocab_log(today=today_iso(), rows=[row])
        _session_new_words_logged.append(row)

    return jsonify({"ok": True, "new_state": new.state})


@app.route("/api/undo", methods=["POST"])
def api_undo():
    """Pop the last grade and re-display the restored card.

    Chains: each call undoes one more card. Adjusts session counters so the
    progress bar reflects the rewind. Returns the restored card so the
    frontend can render it on its front and let the user re-grade.
    """
    global _session_new_seen, _session_correct, _session_wrong
    global _session_new_words_logged

    conn = db.connect()
    db.init_db(conn)
    res = db.undo_last_review(conn)
    conn.commit()
    if res is None:
        return jsonify({"ok": False, "reason": "no history"}), 404

    if res["grade"] == 1:
        _session_wrong = max(0, _session_wrong - 1)
    else:
        _session_correct = max(0, _session_correct - 1)
    if res["was_new"]:
        _session_new_seen = max(0, _session_new_seen - 1)
        # Find the buffered row to derive the exact word string used in the
        # markdown log, then retract it. We try the buffer first because it
        # has the canonical 'word' value with article. If the buffer was
        # cleared (eg server restart), fall back to building it from the DB.
        buffered = next(
            (r for r in _session_new_words_logged if r.get("word_id") == res["word_id"]),
            None,
        )
        if buffered is not None:
            word_str = buffered["word"]
            _session_new_words_logged = [
                r for r in _session_new_words_logged
                if r.get("word_id") != res["word_id"]
            ]
        else:
            w = db.fetch_word(conn, res["word_id"])
            word_str = (w.get("article") + " " if w.get("article") else "") + w["lemma"]
        retract_from_vocab_log(today_iso(), word_str)

    payload = _card_payload(conn, res["word_id"])
    return jsonify({
        "ok": True,
        "card": payload,
        "is_new": res["was_new"],
        "history": db.history_count(conn),
    })


@app.route("/api/stats")
def api_stats():
    conn = db.connect()
    db.init_db(conn)
    return jsonify(db.stats(conn))


@app.route("/api/history")
def api_history():
    """Return every word in the deck with its primary sentence and translation.

    Used by the History view in the frontend, which renders a table the user
    can scan to see what has been added so far. Ordered by frequency rank so
    the most common words sit at the top.
    """
    conn = db.connect()
    db.init_db(conn)
    rows = conn.execute(
        """
        SELECT w.id, w.rank, w.lemma, w.article, w.pos, w.english,
               w.audio_path, r.state
        FROM words w
        LEFT JOIN reviews r ON r.word_id = w.id
        ORDER BY w.rank ASC
        """
    ).fetchall()
    items = []
    for row in rows:
        sents = db.fetch_sentences(conn, int(row["id"]))
        primary = next((s for s in sents if s["is_primary"]), sents[0] if sents else None)
        items.append({
            "word_id": int(row["id"]),
            "rank": row["rank"],
            "lemma": row["lemma"],
            "article": row["article"],
            "pos": row["pos"],
            "english": row["english"],
            "state": row["state"] or "new",
            "dutch_sentence": primary["dutch"] if primary else "",
            "english_sentence": primary["english"] if primary else "",
            "all_sentences": sents,
            # audio_path is the static-relative URL the play button uses,
            # null for words that have not been generated yet.
            "audio_path": row["audio_path"],
            # Sentence-level audio for the primary example, displayed
            # next to the dutch_sentence column in the History table.
            "dutch_sentence_audio": (primary or {}).get("audio_path"),
        })
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/history/streak")
def api_history_streak():
    """Return per-day review counts and streak metrics for the History tab.

    The frontend renders these as a 12-week heatmap above the words table,
    so the response shape is fixed: 84 day buckets ending today, plus three
    summary numbers. Reviewed_at is stored UTC, day-of-review is interpreted
    in the timezone configured by DUTCH_SRS_TZ (default Europe/Amsterdam) so
    a card graded at 23:30 local time counts toward that calendar day, not
    the next.
    """
    import os as _os
    from datetime import date, datetime, timedelta, timezone as _tz
    tz_name = _os.environ.get("DUTCH_SRS_TZ", "Europe/Amsterdam")
    try:
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo(tz_name)
    except Exception:
        local_tz = _tz.utc

    today = datetime.now(local_tz).date()
    # Window starts at April 29 of the current year (the start of the
    # current study cycle, anything earlier is pre-cycle scratch data
    # and is intentionally hidden from the visualization) and extends
    # two weeks past today so a few upcoming days are visible as
    # inert "future" cells. If today is before April 29 of this year,
    # fall back to last year's April 29 so the window is always
    # populated. The frontend paints any date past today with the
    # .future class, so the future portion of the window reads as a
    # quiet preview rather than active data.
    year = today.year
    start_candidate = date(year, 4, 29)
    if today < start_candidate:
        start_candidate = date(year - 1, 4, 29)
    window_start = start_candidate
    # Extend the right edge of the window well past today so the
    # heatmap renders enough additional empty future weeks to fill
    # the panel horizontally instead of huddling in the left corner.
    # 105 days lets the panel reach roughly 15 weeks at the standard
    # cell size, which fills the typical column comfortably without
    # making the cells themselves chunky. The bar chart spans the
    # same window; bars on future days are zero-height so the future
    # portion reads as blank space, the same as the heatmap.
    window_end = today + timedelta(days=105)

    conn = db.connect()
    db.init_db(conn)
    rows = conn.execute(
        "SELECT reviewed_at, grade FROM review_log ORDER BY reviewed_at ASC"
    ).fetchall()

    by_date: dict[str, int] = {}
    by_date_grades: dict[str, dict[str, int]] = {}
    # Grade totals across the active window only. Reviews logged before
    # window_start are pre-cycle scratch data and are excluded from
    # both the per-day counts and the lifetime breakdown so the
    # History view never reports practice that the user has chosen
    # not to count.
    totals_by_grade: dict[str, int] = {"1": 0, "2": 0, "3": 0, "4": 0}
    for row in rows:
        raw = row["reviewed_at"]
        try:
            dt = db.parse_iso(raw).astimezone(local_tz)
        except Exception:
            continue
        d = dt.date()
        if d < window_start:
            continue
        key = d.isoformat()
        by_date[key] = by_date.get(key, 0) + 1
        bucket = by_date_grades.setdefault(key, {"1": 0, "2": 0, "3": 0, "4": 0})
        g = str(int(row["grade"]))
        if g in bucket:
            bucket[g] += 1
            totals_by_grade[g] += 1
    days = []
    cursor = window_start
    while cursor <= window_end:
        iso_d = cursor.isoformat()
        days.append({
            "date": iso_d,
            "count": by_date.get(iso_d, 0),
            "grades": by_date_grades.get(iso_d, {"1": 0, "2": 0, "3": 0, "4": 0}),
        })
        cursor += timedelta(days=1)

    # Current streak. Count consecutive days ending today. If today has
    # not been reviewed yet, fall back to a streak ending yesterday so a
    # morning visit before the first review still shows the active run.
    current = 0
    cursor = today
    if by_date.get(cursor.isoformat(), 0) == 0:
        cursor -= timedelta(days=1)
    while by_date.get(cursor.isoformat(), 0) > 0:
        current += 1
        cursor -= timedelta(days=1)

    # Longest streak across all of history, not just the visible window.
    longest = 0
    if by_date:
        sorted_keys = sorted(by_date.keys())
        run = 1
        prev = date.fromisoformat(sorted_keys[0])
        longest = 1
        for k in sorted_keys[1:]:
            d = date.fromisoformat(k)
            if (d - prev).days == 1:
                run += 1
            else:
                run = 1
            if run > longest:
                longest = run
            prev = d

    return jsonify({
        "days": days,
        "today": today.isoformat(),
        "current_streak": current,
        "longest_streak": longest,
        "total_reviews": sum(by_date.values()),
        "totals_by_grade": totals_by_grade,
    })


def _difficulty_weight(state: str, lapses: int, ease: float, *, extra: float = 0.0) -> float:
    """Compose the soft sampling weight used by both production drills.

    Base 1.0, plus a lapse term, plus an ease-deficit term. The state term is
    zero in the new gating regime since both drills only see graduated
    sentences (state='review'), but we keep the dispatch in case future work
    relaxes that gate. `extra` is reserved for per-drill bias, eg the Word
    order failure boost. See Methodology.md section 11 and the existing
    Sentence forming docstring for the rationale.
    """
    w = 1.0
    w += 2.0 * lapses
    w += 2.0 * max(0.0, 3.0 - ease)
    if state == "learning":
        w += 3.0
    elif state == "relearning":
        w += 4.0
    w += extra
    return w


def _weighted_sample(items: list[dict], weights: list[float], count: int) -> list[dict]:
    """Efraimidis-Spirakis A-Res weighted sample without replacement.

    Each item gets key `random() ** (1 / weight)`, the top `count` keys win.
    Returns the chosen items in shuffled order so the queue does not betray
    its sampling structure to the user.
    """
    import random
    if not items:
        return []
    keyed = []
    for idx, w in enumerate(weights):
        u = random.random()
        if u <= 0.0:
            u = 1e-12
        key = u ** (1.0 / max(w, 1e-3))
        keyed.append((key, idx))
    keyed.sort(reverse=True)
    chosen = [items[i] for _, i in keyed[:count]]
    random.shuffle(chosen)
    return chosen


@app.route("/api/sentence_forming/queue")
def api_sentence_forming_queue():
    """Return a randomized subset of started sentences biased toward harder items.

    The Sentence forming exercise is a production drill from English to Dutch
    over sentences whose word the user has started, ie state in
    ('learning','review','relearning'). Two design goals shape this endpoint.

    First, each session should not start from the same list. Without
    randomness the user drills the same sentences in the same order every
    time, which lets pure ordering memory leak in. We sample `count` items
    fresh on every request, so refreshing the queue shuffles both the
    selection and the display order.

    Second, the queue should bias toward sentences the user has found hard.
    Lapses and lower ease push the item up in the sample. The bias is a soft
    weight, easy items still appear regularly so the deck does not collapse
    to just the failure cases.

    Eligibility uses the shared db.eligible_sentences_for_drill helper, see
    Methodology.md section 11. Both production drills draw from the same
    pool, gated only by "the headword has been seen at least once".

    Query params:
        count : maximum sentences to return, default 25.

    Each item carries an ordered variations list, the near-variations that
    swap one element of the parent sentence at a time. The frontend reveals
    them one by one after the original Dutch is uncovered. See
    Methodology.md, Generalization through near-variations, for the
    rationale.
    """
    count = max(1, int(request.args.get("count", 25)))

    conn = db.connect()
    db.init_db(conn)
    rows = db.eligible_sentences_for_drill(conn, require_gloss=False)

    items: list[dict] = []
    weights: list[float] = []
    for r in rows:
        sent_id = int(r["sentence_id"])
        state = r["state"]
        lapses = int(r["lapses"] or 0)
        ease = float(r["ease"] or 2.5)
        w = _difficulty_weight(state, lapses, ease)
        weights.append(w)
        items.append({
            "sentence_id": sent_id,
            "word_id": int(r["word_id"]),
            "lemma": r["lemma"],
            "article": r["article"],
            "pos": r["pos"],
            "word_english": r["word_english"] or "",
            "word_audio_path": r["word_audio_path"] or "",
            "sentence_audio_path": r["sentence_audio_path"] or "",
            "sense": r["sense"],
            "dutch": r["dutch"],
            "english": r["english"],
            "tense": r["tense"],
            "form": r["form"],
            "state": state,
            "lapses": lapses,
            "ease": round(ease, 3),
            "difficulty_weight": round(w, 3),
            "variations": db.fetch_variations(conn, sent_id),
        })

    if not items:
        return jsonify({"items": [], "count": 0, "available": 0})

    sampled = _weighted_sample(items, weights, count)
    return jsonify({
        "items": sampled,
        "count": len(sampled),
        "available": len(items),
    })


# ---------- Word order drill ----------
#
# The Word order tab is a structural reordering exercise. The user sees the
# English sentence on top, the Dutch tokens below in English word order
# (literal_gloss), and drags them into the canonical Dutch arrangement. See
# Methodology.md section 11 for the design rationale. The endpoints here
# mirror Sentence forming: a queue endpoint that samples started sentences
# weighted by difficulty plus a Word-order failure boost, and a grade endpoint
# that records the attempt without touching the SM-2 scheduler.


import re as _re

_PUNCT_RE = _re.compile(r"[.?!,;:]+")


def _tokenize_dutch(text: str) -> list[str]:
    """Split a Dutch sentence into the tokens the drill operates on.

    Punctuation is stripped uniformly: any run of `.?!,;:` characters is
    replaced with a space, then the result is whitespace-split. So the
    canonical Dutch and the literal_gloss can both be authored with or
    without punctuation and they tokenize to the same multiset. Internal
    apostrophes (eg 's avonds) and hyphens (eg interne-loop) stay attached
    because they are not in the strip set.

    The drill's pills are these tokens, so the user never sees a stray
    comma or question mark stuck to a word. The canonical sentence with
    its punctuation remains in the DB and is shown verbatim in the
    canonical-reveal block after a pass.
    """
    cleaned = _PUNCT_RE.sub(" ", text)
    return cleaned.split()


@app.route("/api/word_order/queue")
def api_word_order_queue():
    """Return a randomized subset of started, glossed sentences.

    Eligibility comes from db.eligible_sentences_for_drill with
    require_gloss=True, ie any sentence whose headword the user has started
    AND whose literal_gloss is authored. Sampling uses the same A-Res
    weighted sampler as Sentence forming, with an additional positive bias
    for sentences the user has previously failed in this drill, see
    Methodology section 11.

    Each item carries:
        sentence_id, word_id, lemma, article, pos, sense
        english          the English prompt
        dutch            canonical Dutch, kept server-side for grading
        canonical_tokens canonical Dutch tokenized
        gloss_tokens     literal_gloss tokenized, the draggable pills in
                         English word order, the user's starting state
        tense, form, level
        difficulty_weight

    Query params:
        count : maximum sentences to return, default 20.
    """
    count = max(1, int(request.args.get("count", 20)))

    conn = db.connect()
    db.init_db(conn)
    rows = db.eligible_sentences_for_drill(conn, require_gloss=True)
    fail_counts = db.word_order_failure_counts(conn)

    items: list[dict] = []
    weights: list[float] = []
    for r in rows:
        sent_id = int(r["sentence_id"])
        canonical_tokens = _tokenize_dutch(r["dutch"])
        gloss_tokens = _tokenize_dutch(r["literal_gloss"] or "")
        # Discard rows whose gloss does not survive validation. The build
        # script enforces this invariant on write, but a stale row from a
        # legacy seed pass might violate it. Rather than crash the endpoint,
        # we drop the offender and continue.
        if sorted(canonical_tokens) != sorted(gloss_tokens):
            continue

        state = r["state"]
        lapses = int(r["lapses"] or 0)
        ease = float(r["ease"] or 2.5)
        # Failures bias the same sentence to come back sooner. Soft, capped.
        fail_boost = 1.5 * min(int(fail_counts.get(sent_id, 0)), 4)
        w = _difficulty_weight(state, lapses, ease, extra=fail_boost)
        weights.append(w)
        items.append({
            "sentence_id": sent_id,
            "word_id": int(r["word_id"]),
            "lemma": r["lemma"],
            "article": r["article"],
            "pos": r["pos"],
            "word_english": r["word_english"] or "",
            "word_audio_path": r["word_audio_path"] or "",
            "sentence_audio_path": r["sentence_audio_path"] or "",
            "sense": r["sense"],
            "english": r["english"],
            "dutch": r["dutch"],
            "canonical_tokens": canonical_tokens,
            "gloss_tokens": gloss_tokens,
            "tense": r["tense"],
            "form": r["form"],
            "level": r["level"],
            "state": state,
            "fail_count": int(fail_counts.get(sent_id, 0)),
            "difficulty_weight": round(w, 3),
        })

    if not items:
        return jsonify({"items": [], "count": 0, "available": 0})

    sampled = _weighted_sample(items, weights, count)
    return jsonify({
        "items": sampled,
        "count": len(sampled),
        "available": len(items),
    })


@app.route("/api/word_order/grade", methods=["POST"])
def api_word_order_grade():
    """Record one Word order attempt and return whether it matched the canonical order.

    Body:
        {
            "sentence_id": int,
            "submitted_order": ["Ik", "ga", "morgen", "naar", "de", "markt"]
        }

    The server is the source of truth for the canonical Dutch tokenization,
    the client should not be trusted to grade itself. The response carries
    `ok`, the canonical order, the misplaced indices, and a one-line
    structural hint when the attempt failed. The grade does not move the
    SM-2 state of the headword, see Methodology section 11.
    """
    data = request.get_json(force=True) or {}
    sentence_id = int(data.get("sentence_id") or 0)
    submitted = data.get("submitted_order") or []
    if not sentence_id or not isinstance(submitted, list):
        return jsonify({"error": "sentence_id and submitted_order are required"}), 400

    conn = db.connect()
    db.init_db(conn)
    row = conn.execute(
        """
        SELECT s.id, s.word_id, s.dutch, s.english, s.tense, s.form
        FROM sentences s
        WHERE s.id = ?
        """,
        (sentence_id,),
    ).fetchone()
    if row is None:
        return jsonify({"error": "unknown sentence"}), 404

    canonical = _tokenize_dutch(row["dutch"])
    if sorted(canonical) != sorted([str(t) for t in submitted]):
        # The pills the user submitted are not even a permutation of the
        # canonical tokens. This should be impossible from the UI but guard
        # against tampered requests so we never log a meaningless attempt.
        return jsonify({"error": "submitted tokens do not match the sentence"}), 400

    submitted_str = [str(t) for t in submitted]
    misplaced = [i for i in range(len(canonical)) if submitted_str[i] != canonical[i]]
    ok = len(misplaced) == 0
    db.log_word_order_attempt(
        conn,
        sentence_id=sentence_id,
        word_id=int(row["word_id"]),
        when=datetime.now(timezone.utc),
        ok=ok,
        submitted_order=submitted_str,
        canonical_order=canonical,
        misplaced_count=len(misplaced),
    )
    conn.commit()

    hint = None if ok else _word_order_hint(canonical, submitted_str, row)
    return jsonify({
        "ok": ok,
        "canonical_order": canonical,
        "misplaced_indices": misplaced,
        "hint": hint,
    })


def _word_order_hint(
    canonical: list[str], submitted: list[str], row: dict,
) -> str:
    """Pick a one-line structural hint that fits the user's mistake.

    The hints are intentionally short and pedagogical, not comprehensive. The
    Word order tab leaves room for the user to click and leave a feedback
    note when they want a deeper explanation. The matching is heuristic, we
    look for the most likely structural failure mode and surface that rule.
    Order of checks matters, the first match wins.

    Heuristics:
        - subordinate clause cue word in position 1, finite verb not at end
          -> verb-final hint
        - non-subject in position 1, subject before the first verb
          -> V2 inversion hint
        - separable particle adjacent to its verb instead of trailing
          -> separable verb hint
        - perfectum auxiliary present, participle not at end
          -> perfectum stack hint
        - default
          -> "the finite verb in a main clause sits in position 2"
    """
    sub_cues = {"omdat", "dat", "als", "toen", "hoewel", "terwijl",
                "zodat", "voordat", "nadat", "wanneer", "of"}
    if canonical and canonical[0].lower() in sub_cues:
        return "subordinate clause, the verb stack moves to the end"
    # Inversion cue: position 1 is a non-subject (time, place, object), so
    # the canonical pattern is [filler] [V-fin] [subject] ...
    pronouns = {"ik", "jij", "je", "u", "hij", "zij", "ze", "het", "we", "wij", "jullie"}
    if canonical and canonical[0].lower() not in pronouns and len(canonical) >= 3:
        return "the finite verb sits in position 2, subject moves after it (V2 inversion)"
    if any(t.lower() in {"heb", "hebt", "heeft", "hebben", "ben", "is", "zijn", "was", "waren"} for t in canonical):
        # Perfectum or copula stack, the participle or non-finite piece tends
        # to land at the right edge.
        return "the participle or second verb piece goes to the end"
    return "the finite verb in a main clause sits in position 2"


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    """Append a user note to the Feedback Log markdown file.

    The endpoint is intentionally minimal. It captures the lemma the user
    clicked on, their free-text note, and a timestamp, then appends to the
    markdown log under DUTCH_SRS_VAULT/SRS/Feedback Log.md. No automatic
    explanation is produced, that is reserved for follow-up sessions where
    the user points an LLM at the entry. When DUTCH_SRS_VAULT is unset the
    note is accepted by the API but not persisted to disk.

    Body:
        {
            "lemma": "terug",
            "dutch": "Ik ben zo terug.",
            "english": "I will be right back.",   # optional
            "note": "This doesn't feel natural to me, weird structure.",
            "source": "sentence forming"          # 'new words' | 'sentence forming' | 'variation'
        }
    """
    data = request.get_json(force=True) or {}
    lemma = (data.get("lemma") or "").strip()
    dutch = (data.get("dutch") or "").strip()
    note = (data.get("note") or "").strip()
    source = (data.get("source") or "new words").strip()
    english = data.get("english")
    if english is not None:
        english = english.strip()

    if not lemma or not dutch or not note:
        return jsonify({"error": "lemma, dutch, and note are required"}), 400

    res = append_to_feedback_log(
        lemma=lemma,
        dutch=dutch,
        english=english,
        note=note,
        source=source,
    )
    return jsonify({"ok": True, "stamp": res["stamp"], "path": res["path"]})


@app.route("/api/custom_sentence", methods=["POST"])
def api_custom_sentence():
    """Add a user-authored sentence to the deck from the web UI.

    Mirrors cli.cmd_add but accepts a JSON body and optionally takes a list
    of two near-variations so the new sentence is immediately ready for the
    Sentence forming flow with full structural drill support.

    Body:
        {
            "lemma": "fiets",
            "article": "de" | "het" | null,
            "english": "bike",
            "notes": "...",
            "sentence_nl": "Ik pak mijn fiets en ga naar de markt.",
            "sentence_en": "I grab my bike and head to the market.",
            "literal_gloss": "Ik pak mijn fiets en ga naar de markt",
            "variations": [
                {"nl": "...", "en": "...", "varied": "noun"},
                {"nl": "...", "en": "...", "varied": "time"}
            ]
        }

    Words added through this endpoint use rank 9999 so they sort to the end
    of the new queue. The endpoint is idempotent on the lemma, calling it
    again with the same lemma replaces the existing sentence and variations.

    The literal_gloss field is optional. When present it must be a permutation
    of the canonical Dutch tokenization, otherwise the request is rejected
    with a 400 so a malformed gloss never reaches the Word order queue. See
    Methodology.md section 11 for what the gloss is and why we author it
    alongside the sentence.
    """
    data = request.get_json(force=True) or {}
    lemma = (data.get("lemma") or "").strip()
    sent_nl = (data.get("sentence_nl") or "").strip()
    sent_en = (data.get("sentence_en") or "").strip()
    if not lemma or not sent_nl or not sent_en:
        return jsonify({"error": "lemma, sentence_nl, and sentence_en are required"}), 400

    article = data.get("article") or None
    english = (data.get("english") or "").strip()
    notes = data.get("notes") or None

    literal_gloss = (data.get("literal_gloss") or "").strip() or None
    if literal_gloss is not None:
        canon_tokens = _tokenize_dutch(sent_nl)
        gloss_tokens = _tokenize_dutch(literal_gloss)
        if sorted(canon_tokens) != sorted(gloss_tokens):
            return jsonify({
                "error": "literal_gloss must be a permutation of the Dutch sentence tokens",
                "canonical_tokens": canon_tokens,
                "gloss_tokens": gloss_tokens,
            }), 400

    raw_vars = data.get("variations") or []
    variations: list[dict] = []
    for v in raw_vars:
        if not isinstance(v, dict):
            continue
        nl = (v.get("nl") or "").strip()
        en = (v.get("en") or "").strip()
        if not nl or not en:
            continue
        variations.append({
            "nl": nl,
            "en": en,
            "varied": (v.get("varied") or "").strip() or None,
        })

    conn = db.connect()
    db.init_db(conn)
    # rank=0 puts user-added words ahead of every seed entry in the
    # new-card queue, which is what new_word_ids relies on to surface
    # custom additions first. Seed words use rank >= 1.
    wid = db.upsert_word(
        conn, rank=0, lemma=lemma, pos=None, article=article,
        english=english, notes=notes, added_on=today_iso(),
    )
    db.replace_sentences(conn, word_id=wid, sentences=[{
        "nl": sent_nl,
        "en": sent_en,
        "sense": None,
        "level": data.get("level") or "A2",
        "literal_gloss": literal_gloss,
        "variations": variations,
    }])
    # Custom additions enter the new queue right away. The user explicitly
    # asked for this sentence, so we want it at the head of practice when
    # they next review, not buried below frequency-ranked seeds.
    from datetime import datetime as _dt, timezone as _tz
    db.get_or_create_review(conn, wid, due_at=_dt.now(_tz.utc))
    conn.commit()
    return jsonify({
        "ok": True,
        "word_id": wid,
        "lemma": lemma,
        "variations_count": len(variations),
    })


@app.route("/api/session")
def api_session():
    """Return per-session counters that drive the on-screen progress bar."""
    _reset_if_new_day()
    total = _session_correct + _session_wrong
    pct = (100 * _session_correct // total) if total else 0
    return jsonify({
        "today": _session_today,
        "new_seen": _session_new_seen,
        "correct": _session_correct,
        "wrong": _session_wrong,
        "pass_pct": pct,
        "total_reviewed": total,
    })


@app.route("/api/finish", methods=["POST"])
def api_finish():
    """Write the daily-note summary at end of session.

    Per-card Vocab Log writes happen in /api/grade now, so this endpoint is
    just for the aggregate one-line summary on today's daily note. Called by
    the frontend when the queue empties.
    """
    global _session_new_words_logged
    today = today_iso()
    total = _session_correct + _session_wrong
    pct = (100 * _session_correct // total) if total else 0
    n_new = len(_session_new_words_logged)
    summary = f"SRS web: {total} cards, {pct}% pass, {n_new} new words"
    append_to_daily(today=today, summary=summary)
    _session_new_words_logged = []
    return jsonify({"summary": summary, "new_words": n_new})


def run(host: str = "127.0.0.1", port: int = 5051) -> None:
    """Start the dev server. Production mode is overkill for a single-user
    local tool, the Flask dev server is fine.
    """
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    run()
