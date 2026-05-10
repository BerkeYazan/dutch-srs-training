"""Optional integration layer for an external knowledge vault.

Originally written against an Obsidian vault that owned the Dutch curriculum.
In the open-source build the vault is opt-in: when DUTCH_SRS_VAULT is set to a
folder, the three writers below append into that folder using the same file
layout. When the env var is unset, every writer is a no-op so the app runs
standalone without a vault.

Three integration points, all append-only:

1. Vocab Log. A markdown table of every new word the user has met, grouped by
   day. Path: $DUTCH_SRS_VAULT/Vocab/Vocab Log.md
2. Daily note. A one-line session summary appended to today's daily journal
   only if a file at $DUTCH_SRS_VAULT/Daily/YYYY-MM-DD.md already exists.
3. Feedback Log. Confusion notes the user types in the web UI. Path:
   $DUTCH_SRS_VAULT/SRS/Feedback Log.md

The default deployment writes nothing to disk outside the SQLite database.
Set DUTCH_SRS_VAULT if you want the markdown trail on top.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path


def _vault_root() -> Path | None:
    """Resolve the configured vault root, or return None when disabled.

    The env var is read on every call so tests can flip it without reloading
    the module. Returns None when the var is missing or points at a path that
    does not exist, in which case all writers turn into no-ops.
    """
    raw = os.environ.get("DUTCH_SRS_VAULT")
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.exists():
        return None
    return p


def _vocab_log_path() -> Path | None:
    root = _vault_root()
    return None if root is None else root / "Vocab" / "Vocab Log.md"


def _daily_dir() -> Path | None:
    root = _vault_root()
    return None if root is None else root / "Daily"


def _feedback_log_path() -> Path | None:
    root = _vault_root()
    return None if root is None else root / "SRS" / "Feedback Log.md"


def _ensure_today_heading(text: str, today: str) -> tuple[str, bool]:
    """Return (text, created) where text is guaranteed to contain '## {today}'.

    'Entries by Theme' anchor is a stable footer in Vocab Log. We insert before
    it. If the file lacks that anchor, we append at the end.
    """
    heading = f"## {today}"
    if heading in text:
        return text, False

    anchor = "## Entries by Theme"
    block = f"\n{heading}\n\n_Auto-appended by SRS during day 1 seeding._\n\n| Word | English | Example | Source | Tag | Notes |\n|---|---|---|---|---|---|\n"
    if anchor in text:
        idx = text.index(anchor)
        return text[:idx] + block + "\n" + text[idx:], True
    return text + "\n" + block, True


def _placeholder_row(today: str) -> str:
    return f"_Auto-appended by SRS during day 1 seeding._\n"


def append_to_vocab_log(
    *,
    today: str,
    rows: list[dict],
) -> None:
    """Append rows under today's date heading in Vocab Log.

    rows is a list of dicts with keys: word, english, example, source, tag, notes.
    Existing rows are not removed. Duplicates are not deduplicated, this is a
    log, not a deck. No-op when DUTCH_SRS_VAULT is unset.
    """
    vocab_log = _vocab_log_path()
    if vocab_log is None:
        return
    if not vocab_log.exists():
        vocab_log.parent.mkdir(parents=True, exist_ok=True)
        vocab_log.write_text(f"# Vocab Log\n\n## {today}\n\n| Word | English | Example | Source | Tag | Notes |\n|---|---|---|---|---|---|\n")

    text = vocab_log.read_text()
    text, _ = _ensure_today_heading(text, today)

    # Find the last table row under today's heading and append below it. We do a
    # simple split-and-rebuild rather than a fancy markdown parse.
    heading = f"## {today}"
    parts = text.split(heading, 1)
    head, rest = parts[0], parts[1]

    # Within rest, find the next '## ' heading boundary.
    next_idx = rest.find("\n## ")
    today_block = rest if next_idx == -1 else rest[:next_idx]
    after_block = "" if next_idx == -1 else rest[next_idx:]

    # Build new rows. If today's block has no table, _ensure_today_heading
    # already inserted one.
    new_rows = []
    for r in rows:
        cells = [
            r.get("word", "").replace("|", "\\|"),
            r.get("english", "").replace("|", "\\|"),
            r.get("example", "").replace("|", "\\|"),
            r.get("source", "SRS day 1"),
            r.get("tag", "#srs #core1k"),
            r.get("notes", "") or "",
        ]
        new_rows.append("| " + " | ".join(cells) + " |")
    addition = "\n".join(new_rows) + "\n"

    today_block = today_block.rstrip() + "\n" + addition + "\n"
    new_text = head + heading + today_block + after_block
    vocab_log.write_text(new_text)


def retract_from_vocab_log(today: str, word: str) -> bool:
    """Remove the most recent row whose first cell equals `word` under today's
    heading. Returns True if a row was removed. Idempotent: returns False if
    no match was found or the file does not exist.

    Used by the undo path to keep the markdown log in sync with the SRS state
    when the user takes back a new-card grade. Match is exact on the first
    cell, which is what append_to_vocab_log writes. Returns False when the
    vault integration is disabled.
    """
    vocab_log = _vocab_log_path()
    if vocab_log is None or not vocab_log.exists():
        return False
    text = vocab_log.read_text()
    heading = f"## {today}"
    if heading not in text:
        return False
    head, _, rest = text.partition(heading)
    next_idx = rest.find("\n## ")
    block = rest if next_idx == -1 else rest[:next_idx]
    after = "" if next_idx == -1 else rest[next_idx:]

    lines = block.split("\n")
    target_idx = None
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")]
        # cells[0] is empty (leading |), cells[1] is the first column
        if len(cells) >= 2 and cells[1] == word:
            # do not delete the header row '| Word | English | ...'
            if cells[1].lower() == "word":
                continue
            target_idx = i
            break
    if target_idx is None:
        return False
    del lines[target_idx]
    block = "\n".join(lines)
    vocab_log.write_text(head + heading + block + after)
    return True


def append_to_daily(today: str, summary: str) -> None:
    """Append a one-line SRS summary to today's daily note if it exists.

    We do not create the daily note, that file's lifecycle is owned by the
    user's note system, not by the SRS app. No-op without a vault.
    """
    daily_dir = _daily_dir()
    if daily_dir is None:
        return
    f = daily_dir / f"{today}.md"
    if not f.exists():
        return
    text = f.read_text()
    section = "\n\n## SRS\n"
    if "## SRS" not in text:
        text = text.rstrip() + section
    text = text.rstrip() + f"\n- {summary}\n"
    f.write_text(text)


def today_iso() -> str:
    return date.today().isoformat()


def append_to_feedback_log(
    *,
    lemma: str,
    dutch: str,
    english: str | None,
    note: str,
    source: str,
    when: datetime | None = None,
) -> dict:
    """Append a confusion note to Feedback Log.md.

    The entry is grow-only. We never edit existing notes from this code path,
    only append new ones, so the chronological history of what tripped the
    user up stays intact. Explanations are added by an LLM only on explicit
    request, in a separate manual edit pass that appends an
    **Explanation** block under the entry.

    Args:
        lemma:   the headword the click originated from.
        dutch:   the Dutch word or sentence being annotated.
        english: optional English gloss or translation of the same item.
        note:    user's free-text note on what feels off or unclear.
        source:  short label for where the click happened, eg
                 'new words', 'sentence forming', 'variation'.
        when:    timestamp, defaults to now in local time.

    Returns:
        dict with the rendered entry text and the file path written, useful
        for the API response. Returns {"path": None} when the vault is
        disabled, so the API can still respond cleanly.
    """
    ts = when or datetime.now()
    stamp = ts.strftime("%Y-%m-%d %H:%M")
    note_clean = note.strip()
    eng_line = f"\n**English**: {english.strip()}" if english and english.strip() else ""
    entry = (
        f"\n### {stamp}, lemma `{lemma}`, source: {source}\n"
        f"**Dutch**: {dutch.strip()}"
        f"{eng_line}\n"
        f"**Note**: {note_clean}\n"
        f"**Explanation**: pending\n"
        "\n---\n"
    )

    feedback_log = _feedback_log_path()
    if feedback_log is None:
        return {"path": None, "stamp": stamp, "entry": entry}

    if not feedback_log.exists():
        feedback_log.parent.mkdir(parents=True, exist_ok=True)
        feedback_log.write_text(
            "---\ntags: [dutch, srs, feedback]\n---\n\n# Feedback Log\n\n## Entries\n"
        )

    text = feedback_log.read_text()
    if "## Entries" not in text:
        text = text.rstrip() + "\n\n## Entries\n"
    # Append at the end, chronological order is preserved without parsing the
    # existing entries. The header block stays untouched.
    text = text.rstrip() + entry
    feedback_log.write_text(text)

    # Post-write verification. The previous silent-no-op bug taught us not
    # to trust that write_text actually persisted bytes to disk under all
    # filesystem conditions. We read the file back and confirm the entry
    # signature is present. The signature is the timestamp lemma source
    # triple, unique per entry. If the read-back fails, raise so the API
    # can return a 500 instead of an optimistic 200.
    verify_signature = f"### {stamp}, lemma `{lemma}`, source: {source}"
    try:
        readback = feedback_log.read_text()
    except OSError as exc:
        raise RuntimeError(
            f"feedback write verification failed, could not read back "
            f"{feedback_log}: {exc}"
        ) from exc
    if verify_signature not in readback:
        raise RuntimeError(
            f"feedback write verification failed, entry not found in "
            f"{feedback_log} after write. signature={verify_signature!r}"
        )

    return {"path": str(feedback_log), "stamp": stamp, "entry": entry}
