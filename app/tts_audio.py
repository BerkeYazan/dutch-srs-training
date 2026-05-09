"""Generate per-lemma audio clips with Google Cloud Text-to-Speech.

Walks the SEED dict in seed_data.py and renders one MP3 per Dutch lemma using
Google Cloud TTS Wavenet voices. Each lemma is deterministically assigned to
either a female (nl-NL-Wavenet-D) or male (nl-NL-Wavenet-B) voice based on a
hash of the lemma string, so re-runs produce the same voice for the same
word and a re-generation only swaps timbre when we explicitly bump the seed.

Outputs land in two places. The canonical copy used by the running Flask app
sits under app/static/audio/words/, served directly by Flask's static
handler at /audio/words/{lemma}.mp3. A second copy mirrors to the vault at
Obsidian Vault/Dutch Learning/Audio/words/, kept as a long-term archive
that can be reused for Anki imports, podcast clips, or any other downstream
project. Both copies are byte-identical.

The script is idempotent: a lemma whose MP3 already exists in the app
static folder is skipped unless --force is passed. After each successful
render, the words.audio_path and words.audio_voice columns are updated so
the API response can carry the correct path through to the frontend.

Run:
    python tts_audio.py --limit 5 --dry-run         # preview the plan
    python tts_audio.py --limit 5                   # generate first five
    python tts_audio.py                             # generate all 200
    python tts_audio.py --lemma aan --force         # re-render one lemma

Setup:
    Set GOOGLE_APPLICATION_CREDENTIALS to the absolute path of your service
    account JSON key. See the comment block below for the one-time GCP
    setup steps. The script aborts with a clear message if the credential
    is missing.

Pointers:
    See SRS/README.md for the wider design and the Methodology note for
    pedagogy. Audio is added because production also benefits from
    pronunciation, not just translation, see Methodology.md.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db  # noqa: E402
from seed_data import SEED  # noqa: E402

# Two-voice canonical pair. Wavenet-D is the most natural-sounding Dutch
# female voice in the current Google catalog, Wavenet-B is the clearest
# male voice. Keeping the pair small means a learner builds expectations
# against two consistent timbres rather than five drifting ones.
VOICE_FEMALE = "nl-NL-Wavenet-D"
VOICE_MALE = "nl-NL-Wavenet-B"
VOICES = (VOICE_FEMALE, VOICE_MALE)

# Output directories. The app copy is served by Flask. An optional archival
# mirror under DUTCH_SRS_VAULT/Audio/{words,sentences}/ is written when the
# vault env var is set, useful if you want to reuse the MP3s in another tool
# (Anki, a podcast clip script). Both copies stay byte-identical because
# every successful render writes to both.
APP_AUDIO_DIR = HERE / "static" / "audio" / "words"
APP_SENTENCE_DIR = HERE / "static" / "audio" / "sentences"


def _vault_audio_dir(kind: str) -> Path | None:
    """Return the archival mirror path, or None when no vault is configured."""
    raw = os.environ.get("DUTCH_SRS_VAULT")
    if not raw:
        return None
    base = Path(raw).expanduser()
    if not base.exists():
        return None
    return base / "Audio" / kind


VAULT_AUDIO_DIR = _vault_audio_dir("words")
VAULT_SENTENCE_DIR = _vault_audio_dir("sentences")


def pick_voice(lemma: str) -> str:
    """Deterministically pick a voice for a lemma.

    Uses random.Random seeded by the lemma string so the assignment is
    stable across runs. Re-running the script for a new lemma never
    perturbs the voice of an old one.
    """
    return random.Random(lemma).choice(VOICES)


def safe_filename(lemma: str) -> str:
    """Return a filesystem-safe MP3 filename for a lemma.

    The seed dict uses bare Dutch lowercase tokens almost everywhere, but
    a defensive sanitizer keeps the script from breaking the day a
    multi-word entry, an apostrophe, or a hyphen sneaks in. Spaces and
    apostrophes collapse to underscores, hyphens are kept, anything
    outside ascii letters or these separators is dropped.
    """
    keep = []
    for ch in lemma.strip().lower():
        if ch.isalpha() or ch in ("-", "_"):
            keep.append(ch)
        elif ch in (" ", "'", "'"):
            keep.append("_")
        # Anything else (digits in odd lemmas, punctuation) is dropped.
    cleaned = "".join(keep) or "lemma"
    return f"{cleaned}.mp3"


# Shared TTS client across worker threads. The google-cloud-texttospeech
# client is documented as thread-safe, and reusing one client avoids paying
# the per-call construction cost (TLS handshake, channel setup) on every
# render. Lock guards lazy init so two threads do not both build a client.
_tts_client = None
_tts_client_lock = Lock()


def _client():
    """Return the lazily-initialized shared TTS client.

    The import is local so the rest of the script (--dry-run, sanity
    checks) can run without google-cloud-texttospeech installed.
    """
    global _tts_client
    if _tts_client is None:
        with _tts_client_lock:
            if _tts_client is None:
                from google.cloud import texttospeech

                _tts_client = texttospeech.TextToSpeechClient()
    return _tts_client


def synthesize(text: str, voice_name: str) -> bytes:
    """Call Google Cloud TTS and return raw MP3 bytes.

    The speaking_rate is held at 1.0, which Wavenet renders as a natural
    conversational pace for Dutch single words. A slower rate sounded
    artificially deliberate in test renders.
    """
    from google.cloud import texttospeech

    client = _client()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="nl-NL",
        name=voice_name,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.0,
        sample_rate_hertz=24000,
    )
    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config,
    )
    return response.audio_content


def write_both_copies(
    filename: str,
    mp3_bytes: bytes,
    *,
    app_dir: Path,
    vault_dir: Path | None,
) -> tuple[Path, Path | None]:
    """Write the MP3 to the app static dir and optionally mirror to a vault.

    Returns (app_path, vault_path). vault_path is None when no vault is
    configured, in which case the app copy is the only artifact. The mirror
    copy uses shutil.copyfile rather than a second write so we cannot
    accidentally produce a different file in the two locations.
    """
    app_dir.mkdir(parents=True, exist_ok=True)
    app_path = app_dir / filename
    app_path.write_bytes(mp3_bytes)
    if vault_dir is None:
        return app_path, None
    vault_dir.mkdir(parents=True, exist_ok=True)
    vault_path = vault_dir / filename
    shutil.copyfile(app_path, vault_path)
    return app_path, vault_path


def already_done(filename: str, *, app_dir: Path, vault_dir: Path | None) -> bool:
    """True when the canonical app copy is on disk, and the vault mirror as
    well if a vault is configured.

    A partial state, app copy present but vault copy missing while a vault is
    configured, is treated as not done so the next run completes the mirror
    without regenerating.
    """
    if not (app_dir / filename).exists():
        return False
    if vault_dir is None:
        return True
    return (vault_dir / filename).exists()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Generate at most N lemmas in this run. Default: all.",
    )
    parser.add_argument(
        "--lemma",
        type=str,
        default=None,
        help="Generate only this lemma. Useful for re-rendering one word.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-render even when the MP3 already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without calling Google or writing files.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help=(
            "Parallel worker threads for the TTS API. Default 6, which keeps "
            "the burst rate below Google's per-minute Wavenet quota with "
            "headroom. Set to 1 to disable parallelism."
        ),
    )
    parser.add_argument(
        "--scope",
        choices=("words", "sentences", "all"),
        default="all",
        help=(
            "What to render. 'words' generates per-lemma audio only, "
            "'sentences' generates per-sentence audio only, 'all' "
            "(default) does both, words first then sentences."
        ),
    )
    parser.add_argument(
        "--sentence-id",
        type=int,
        default=None,
        help="Render only this single sentence id. Useful for re-rendering one row.",
    )
    args = parser.parse_args()

    if not args.dry_run and "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
        print(
            "GOOGLE_APPLICATION_CREDENTIALS is not set. Point it at your "
            "service account JSON key and re-run. See the docstring at the "
            "top of this file for the one-time setup steps.",
            file=sys.stderr,
        )
        return 2

    conn = db.connect()
    db.init_db(conn)

    total_rendered = 0
    total_skipped = 0
    total_failed = 0

    if args.scope in ("words", "all"):
        r, s, f = run_words(conn, args)
        total_rendered += r
        total_skipped += s
        total_failed += f

    if args.scope in ("sentences", "all"):
        r, s, f = run_sentences(conn, args)
        total_rendered += r
        total_skipped += s
        total_failed += f

    print(
        f"\noverall: rendered={total_rendered} skipped={total_skipped} "
        f"failed={total_failed}"
    )
    return 0 if total_failed == 0 else 1


def run_words(conn, args) -> tuple[int, int, int]:
    """Render audio for lemmas. Returns (rendered, skipped, failed)."""
    # Order by frequency rank, not lemma string. The rank column comes
    # from words.csv at seed time and reflects the OpenSubtitles priority
    # (most frequent first), so a --limit N run hits the top N most useful
    # words. Lemmas missing from the DB
    # (eg, the SEED file is ahead of a reseed) fall through to the end
    # alphabetically as a safety net.
    rank_rows = conn.execute(
        "SELECT lemma, rank FROM words ORDER BY rank ASC"
    ).fetchall()
    ranked = [r["lemma"] for r in rank_rows if r["lemma"] in SEED]
    seen = set(ranked)
    fallback = sorted(l for l in SEED if l not in seen)
    lemmas = ranked + fallback

    if args.lemma:
        if args.lemma not in SEED:
            print(f"Lemma not found in SEED: {args.lemma}", file=sys.stderr)
            return 0, 0, 1
        lemmas = [args.lemma]
    if args.limit is not None:
        lemmas = lemmas[: args.limit]

    print(f"\n--- words pass ({len(lemmas)} candidates) ---")
    rendered = 0
    skipped = 0
    failed = 0
    todo: list[tuple[str, str, str]] = []
    for lemma in lemmas:
        filename = safe_filename(lemma)
        voice = pick_voice(lemma)
        if not args.force and already_done(filename, app_dir=APP_AUDIO_DIR, vault_dir=VAULT_AUDIO_DIR):
            skipped += 1
            print(f"skip   {lemma:<20} {filename}  ({voice}, exists)")
            db.set_word_audio(
                conn,
                lemma=lemma,
                audio_path=f"audio/words/{filename}",
                audio_voice=voice,
            )
            continue
        if args.dry_run:
            print(f"plan   {lemma:<20} {filename}  ({voice})")
            continue
        todo.append((lemma, filename, voice))

    def _render(lemma: str, filename: str, voice: str):
        mp3 = synthesize(lemma, voice)
        write_both_copies(
            filename, mp3, app_dir=APP_AUDIO_DIR, vault_dir=VAULT_AUDIO_DIR
        )
        return lemma, filename, voice, len(mp3)

    if todo:
        t_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futures = {ex.submit(_render, l, f, v): l for (l, f, v) in todo}
            for fut in as_completed(futures):
                lemma = futures[fut]
                try:
                    l, filename, voice, nbytes = fut.result()
                    db.set_word_audio(
                        conn,
                        lemma=l,
                        audio_path=f"audio/words/{filename}",
                        audio_voice=voice,
                    )
                    rendered += 1
                    print(f"ok     {l:<20} {filename}  ({voice}, {nbytes} bytes)")
                except Exception as exc:
                    failed += 1
                    print(f"fail   {lemma:<20} : {exc}", file=sys.stderr)
        elapsed = time.monotonic() - t_start
        rate = rendered / elapsed if elapsed > 0 else 0.0
        print(
            f"words: {rendered} rendered in {elapsed:.1f}s, "
            f"{rate:.1f} clips/sec, workers={args.workers}"
        )
    print(f"words done. rendered={rendered} skipped={skipped} failed={failed}")
    return rendered, skipped, failed


def run_sentences(conn, args) -> tuple[int, int, int]:
    """Render audio for sentences, ordered by their parent word's rank.

    Sentence audio is keyed on sha1(dutch_text)[:16] rather than on the
    SQLite-assigned sentence id. The id varies between fresh seeds and
    across machines, the text does not, so hashing the canonical Dutch
    string is the only key that lets the resulting MP3s ship in the
    repo and link correctly anywhere they are cloned. If the Dutch text
    changes later, the hash changes too, the old MP3 becomes orphaned
    and the next --force rerun renders the new one.

    Voice is picked deterministically from a hash of the Dutch text so
    primary sentences and their variations get a chance at different
    timbres without losing reproducibility.
    """
    from cli import sentence_audio_hash
    # Order: parent word's rank ascending, then primary first within a
    # word, then sort_order. Limits the workload to the most useful
    # sentences first, same logic as the words pass.
    if args.sentence_id is not None:
        rows = conn.execute(
            "SELECT s.id, s.dutch FROM sentences s WHERE s.id = ?",
            (args.sentence_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT s.id, s.dutch
            FROM sentences s
            JOIN words w ON w.id = s.word_id
            ORDER BY w.rank ASC, s.is_primary DESC, s.sort_order ASC
            """
        ).fetchall()

    sents = [(int(r["id"]), r["dutch"]) for r in rows]
    if args.limit is not None:
        sents = sents[: args.limit]

    print(f"\n--- sentences pass ({len(sents)} candidates) ---")
    rendered = 0
    skipped = 0
    failed = 0
    todo: list[tuple[int, str, str, str]] = []
    for sid, dutch in sents:
        filename = f"{sentence_audio_hash(dutch)}.mp3"
        voice = pick_voice(dutch)
        if not args.force and already_done(
            filename, app_dir=APP_SENTENCE_DIR, vault_dir=VAULT_SENTENCE_DIR
        ):
            skipped += 1
            print(f"skip   sent#{sid:<6} {filename}  ({voice}, exists)")
            db.set_sentence_audio(
                conn,
                sentence_id=sid,
                audio_path=f"audio/sentences/{filename}",
                audio_voice=voice,
            )
            continue
        if args.dry_run:
            print(f"plan   sent#{sid:<6} {filename}  ({voice})  {dutch[:40]}")
            continue
        todo.append((sid, dutch, filename, voice))

    def _render(sid: int, dutch: str, filename: str, voice: str):
        mp3 = synthesize(dutch, voice)
        write_both_copies(
            filename,
            mp3,
            app_dir=APP_SENTENCE_DIR,
            vault_dir=VAULT_SENTENCE_DIR,
        )
        return sid, filename, voice, len(mp3)

    if todo:
        t_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futures = {ex.submit(_render, sid, d, f, v): sid for (sid, d, f, v) in todo}
            for fut in as_completed(futures):
                sid = futures[fut]
                try:
                    s, filename, voice, nbytes = fut.result()
                    db.set_sentence_audio(
                        conn,
                        sentence_id=s,
                        audio_path=f"audio/sentences/{filename}",
                        audio_voice=voice,
                    )
                    rendered += 1
                    print(f"ok     sent#{s:<6} {filename}  ({voice}, {nbytes} bytes)")
                except Exception as exc:
                    failed += 1
                    print(f"fail   sent#{sid:<6} : {exc}", file=sys.stderr)
        elapsed = time.monotonic() - t_start
        rate = rendered / elapsed if elapsed > 0 else 0.0
        print(
            f"sentences: {rendered} rendered in {elapsed:.1f}s, "
            f"{rate:.1f} clips/sec, workers={args.workers}"
        )
    print(f"sentences done. rendered={rendered} skipped={skipped} failed={failed}")
    return rendered, skipped, failed


if __name__ == "__main__":
    sys.exit(main())
