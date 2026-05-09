"""One-shot rename of sentence MP3s from id-keyed to hash-keyed names.

Background. Sentence audio used to be named after the SQLite auto-increment
id of the sentence row (47.mp3, 477.mp3, ...). That key is unstable: a
fresh seed on another machine produces different ids for the same Dutch
sentences, which means a pre-rendered MP3 cannot ship in the repo. The
fix is to key the file by sha1(dutch_text)[:16]. This script does the
rename in place, once, on the machine that already has the id-keyed files.

What it does:

1. Connect to the DB at DUTCH_SRS_DB if set, otherwise the OS-default path
   (the same path the running app uses, see app/db.py).
2. For each row in `sentences`, compute the hash of its Dutch text.
3. For each existing app/static/audio/sentences/<id>.mp3, look up the row,
   compute its hash, rename to <hash>.mp3 in the same directory.
4. Update the `audio_path` column in the DB so the running app picks up
   the new name without a reseed.

Idempotent. Re-running after a partial run finishes the remaining
files. Files that are already hash-named (a 16-hex-char stem) are left
alone. Files whose id has no matching row are left alone too, with a
warning, so you can decide what to do with them.

Usage:

    python scripts/migrate_sentence_audio.py            # do the rename
    python scripts/migrate_sentence_audio.py --dry-run  # preview only

Run from the repo root after pulling this commit. After it finishes,
`git status` will show the renames. Commit them, push, your friends
clone the repo and the audio just works.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "app"
sys.path.insert(0, str(APP))

import db  # noqa: E402
from cli import sentence_audio_hash  # noqa: E402


SENT_DIR = APP / "static" / "audio" / "sentences"
HASH_NAME = re.compile(r"^[0-9a-f]{16}\.mp3$")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rename sentence MP3s from id-keyed to hash-keyed names."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without renaming or writing the DB.",
    )
    args = parser.parse_args()

    if not SENT_DIR.exists():
        print(f"no sentence audio directory at {SENT_DIR}, nothing to do")
        return 0

    conn = db.connect()
    db.init_db(conn)

    # Map: id -> dutch text. We pull the whole table because joining
    # against the file list is cheaper than per-file lookups.
    rows = conn.execute("SELECT id, dutch FROM sentences").fetchall()
    id_to_text = {int(r["id"]): r["dutch"] for r in rows}
    print(f"DB has {len(id_to_text)} sentence rows")

    renamed = 0
    skipped = 0
    orphan = 0
    collided = 0

    for mp3 in sorted(SENT_DIR.glob("*.mp3")):
        if HASH_NAME.match(mp3.name):
            skipped += 1
            continue

        try:
            sid = int(mp3.stem)
        except ValueError:
            print(f"skip   {mp3.name}, stem is not an integer or a 16-hex hash")
            skipped += 1
            continue

        dutch = id_to_text.get(sid)
        if not dutch:
            print(f"orphan {mp3.name}, no row id={sid} in the current DB")
            orphan += 1
            continue

        new_name = f"{sentence_audio_hash(dutch)}.mp3"
        new_path = SENT_DIR / new_name
        rel = f"audio/sentences/{new_name}"

        if new_path.exists() and new_path != mp3:
            print(f"collide {mp3.name} -> {new_name}, target already exists")
            collided += 1
            continue

        if args.dry_run:
            print(f"plan   {mp3.name} -> {new_name}  ({dutch[:50]})")
            continue

        mp3.rename(new_path)
        conn.execute(
            "UPDATE sentences SET audio_path = ? WHERE id = ?", (rel, sid)
        )
        renamed += 1
        print(f"ok     {mp3.name} -> {new_name}")

    if not args.dry_run:
        conn.commit()

    print(
        f"\nrenamed={renamed} skipped={skipped} orphan={orphan} collided={collided}"
    )
    if orphan:
        print(
            "orphans are mp3 files whose numeric id has no matching DB row. "
            "either reseed and rerun, or delete them by hand if they are stale."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
