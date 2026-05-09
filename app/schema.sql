-- Dutch SRS schema. SQLite.
-- See SRS/README.md for the algorithm and design notes.

CREATE TABLE IF NOT EXISTS words (
    id            INTEGER PRIMARY KEY,
    rank          INTEGER NOT NULL,            -- frequency rank from words.csv
    lemma         TEXT NOT NULL UNIQUE,
    pos           TEXT,                        -- spaCy POS, eg VERB, NOUN
    article       TEXT,                        -- de / het / NULL
    english       TEXT NOT NULL,
    notes         TEXT,
    added_on      TEXT NOT NULL,               -- ISO date when first added to deck
    -- Pronunciation. Populated by app/tts_audio.py against Google Cloud TTS.
    -- audio_path is the static-relative URL the frontend hits, eg
    -- 'audio/words/aan.mp3'. Null while audio has not been generated yet,
    -- the play button hides for those rows. audio_voice records which
    -- voice was used so a re-render can be scheduled per voice if we ever
    -- want to swap out the female or male timbre. See SRS/README.md.
    audio_path    TEXT,
    audio_voice   TEXT
);

-- A word can have multiple sentences when it is polysemous. Each sentence
-- carries an optional 'sense' label, eg 'still', 'yet', 'more' for nog. The
-- review card shows the primary sentence on the front and all sentences with
-- their sense labels on the back. See SRS/README.md.
--
-- 'tense' and 'form' classify the sentence on the two structural axes the
-- six-variation framework uses, see Methodology.md, section Generalization
-- through near-variations. tense is one of past/present/future, form is one
-- of statement/question. The pair drives which structural variations are
-- generated for this sentence.
CREATE TABLE IF NOT EXISTS sentences (
    id            INTEGER PRIMARY KEY,
    word_id       INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
    dutch         TEXT NOT NULL,
    english       TEXT NOT NULL,
    sense         TEXT,                        -- short sense label, eg 'still'
    level         TEXT,                        -- A1, A2, etc.
    tense         TEXT,                        -- past | present | future
    form          TEXT,                        -- statement | question
    is_primary    INTEGER NOT NULL DEFAULT 0,  -- exactly one primary per word
    sort_order    INTEGER NOT NULL DEFAULT 0,  -- display order within a word
    literal_gloss TEXT,                        -- Dutch tokens in English word order, see Methodology section 11
    -- Pronunciation. Same shape as words.audio_path. Populated by
    -- app/tts_audio.py when --scope is sentences or all. Filename is
    -- the sentence id, eg 'audio/sentences/42.mp3', so the path is
    -- stable across edits to the dutch text.
    audio_path    TEXT,
    audio_voice   TEXT
);

CREATE INDEX IF NOT EXISTS idx_sentences_word ON sentences(word_id);

-- Each sentence ships with a small number of near-variations that swap exactly
-- one element, a noun, a time expression, a person, or a verb. The variations
-- only surface in the Sentence forming view, where the user reveals them one
-- by one after the original Dutch is shown. They are absent from the New words
-- review flow so the SRS card stays anchored to one canonical example. See
-- Dutch Learning/Methodology.md, section Generalization through near-variations,
-- for the pedagogy.
CREATE TABLE IF NOT EXISTS sentence_variations (
    id            INTEGER PRIMARY KEY,
    sentence_id   INTEGER NOT NULL REFERENCES sentences(id) ON DELETE CASCADE,
    dutch         TEXT NOT NULL,
    english       TEXT NOT NULL,
    varied        TEXT,                        -- noun | time | person | verb
    sort_order    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_variations_sentence ON sentence_variations(sentence_id);

-- SRS state. One row per word. SM-2 with learning steps and lapses.
CREATE TABLE IF NOT EXISTS reviews (
    word_id       INTEGER PRIMARY KEY REFERENCES words(id) ON DELETE CASCADE,
    state         TEXT NOT NULL,               -- 'new' | 'learning' | 'review' | 'relearning'
    step          INTEGER NOT NULL DEFAULT 0,  -- index into learning steps
    ease          REAL NOT NULL DEFAULT 2.5,
    interval_days INTEGER NOT NULL DEFAULT 0,
    repetitions   INTEGER NOT NULL DEFAULT 0,
    lapses        INTEGER NOT NULL DEFAULT 0,
    due_at        TEXT NOT NULL,               -- ISO datetime, UTC
    last_reviewed TEXT
);

CREATE INDEX IF NOT EXISTS idx_reviews_due ON reviews(due_at);
CREATE INDEX IF NOT EXISTS idx_reviews_state ON reviews(state);

-- Append-only log for analytics and undo.
-- prev_card_json captures the full Card state before the grade was applied.
-- Undo deletes the latest row and restores the reviews row from this JSON.
CREATE TABLE IF NOT EXISTS review_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id         INTEGER NOT NULL REFERENCES words(id),
    reviewed_at     TEXT NOT NULL,               -- ISO datetime
    grade           INTEGER NOT NULL,            -- 1=again, 2=hard, 3=good, 4=easy
    prev_state      TEXT NOT NULL,
    new_state       TEXT NOT NULL,
    prev_interval   INTEGER NOT NULL,
    new_interval    INTEGER NOT NULL,
    prev_ease       REAL NOT NULL,
    new_ease        REAL NOT NULL,
    prev_card_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_word ON review_log(word_id);
CREATE INDEX IF NOT EXISTS idx_log_time ON review_log(reviewed_at);

-- Settings as a small key-value table so we do not need a config file.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Per-attempt log for the Word order drill. Append-only. Does not feed the
-- SM-2 scheduler, see Methodology.md section 11, that channel stays owned by
-- the New words review flow. The submitted_order and canonical_order columns
-- store JSON arrays of strings so a failed attempt can be replayed and the
-- exact mismatch inspected. The 'ok' flag is denormalized for fast aggregate
-- queries against difficulty.
CREATE TABLE IF NOT EXISTS word_order_attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    sentence_id      INTEGER NOT NULL REFERENCES sentences(id) ON DELETE CASCADE,
    word_id          INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
    attempted_at     TEXT NOT NULL,
    ok               INTEGER NOT NULL,             -- 1 correct, 0 incorrect
    submitted_order  TEXT NOT NULL,                -- JSON array of token strings
    canonical_order  TEXT NOT NULL,                -- JSON array of token strings
    misplaced_count  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_wo_attempts_sentence ON word_order_attempts(sentence_id);
CREATE INDEX IF NOT EXISTS idx_wo_attempts_time     ON word_order_attempts(attempted_at);
