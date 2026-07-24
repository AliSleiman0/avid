-- ─────────────────────────────────────────────────────────────
-- 0001_initial.sql
-- ─────────────────────────────────────────────────────────────

CREATE TABLE facts (
    id                    INTEGER PRIMARY KEY,
    text                  TEXT    NOT NULL,
    kind                  TEXT    NOT NULL
                            CHECK (kind IN ('identity','preference','routine',
                                            'relationship','event','other')),
    importance            INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 10),
    confidence            REAL    NOT NULL DEFAULT 1.0
                            CHECK (confidence BETWEEN 0.0 AND 1.0),

    embedding             BLOB,           -- 384 × float32 LE, pre-normalised

    created_at            INTEGER NOT NULL,
    last_accessed_at      INTEGER NOT NULL,
    access_count          INTEGER NOT NULL DEFAULT 0,

    superseded_by         INTEGER REFERENCES facts(id) ON DELETE SET NULL,
    superseded_at         INTEGER,

    derived_from          TEXT,           -- JSON array of fact ids (reflections, §7.9)
    source_correlation_id TEXT,           -- ties back to episodes / logs (§3.12.2)

    CHECK ((superseded_by IS NULL) = (superseded_at IS NULL))
);

-- THE hot path. §7.7 always filters superseded_by IS NULL; a partial index
-- means the index contains only live facts and stays small forever, however
-- much history accumulates behind it.
CREATE INDEX idx_facts_live
    ON facts(last_accessed_at DESC)
    WHERE superseded_by IS NULL;

CREATE INDEX idx_facts_kind_live
    ON facts(kind)
    WHERE superseded_by IS NULL;

CREATE INDEX idx_facts_superseded_by ON facts(superseded_by);


-- Structured specialisation. Exists ONLY because §10 needs a machine-readable
-- time to schedule. Everything else about a routine lives in facts.text.
CREATE TABLE routines (
    fact_id       INTEGER PRIMARY KEY REFERENCES facts(id) ON DELETE CASCADE,
    rrule         TEXT    NOT NULL,   -- RFC 5545 RRULE, e.g. 'FREQ=DAILY'
    local_time    TEXT    NOT NULL,   -- 'HH:MM' wall-clock
    timezone      TEXT    NOT NULL,   -- IANA, e.g. 'Asia/Beirut'
    lead_time_s   INTEGER NOT NULL DEFAULT 300   -- fire N seconds early
);


CREATE TABLE triggers (
    id             INTEGER PRIMARY KEY,
    fact_id        INTEGER REFERENCES facts(id) ON DELETE CASCADE,
    kind           TEXT    NOT NULL
                     CHECK (kind IN ('schedule','presence','condition')),
    enabled        INTEGER NOT NULL DEFAULT 1,

    next_fire_at   INTEGER,
    last_fired_at  INTEGER,
    fire_count     INTEGER NOT NULL DEFAULT 0,
    ignore_streak  INTEGER NOT NULL DEFAULT 0,   -- §10.5 backoff
    cooldown_s     INTEGER NOT NULL DEFAULT 3600
);

-- The scheduler's only query (§10.3). Partial: disabled and unscheduled
-- triggers never enter the index.
CREATE INDEX idx_triggers_due
    ON triggers(next_fire_at)
    WHERE enabled = 1 AND next_fire_at IS NOT NULL;


-- R-08's instrument. Every proactive DECISION is logged, delivered or not.
CREATE TABLE proactive_log (
    id             INTEGER PRIMARY KEY,
    trigger_id     INTEGER REFERENCES triggers(id) ON DELETE CASCADE,
    considered_at  INTEGER NOT NULL,
    outcome        TEXT    NOT NULL
                     CHECK (outcome IN ('delivered','suppressed')),
    reason         TEXT,        -- which policy rule vetoed (§10.4)
    utterance      TEXT,        -- what it said, or would have said
    user_reaction  TEXT         -- 'engaged'|'ignored'|NULL (unknown yet)
                     CHECK (user_reaction IN ('engaged','ignored') OR user_reaction IS NULL)
);

CREATE INDEX idx_proactive_log_time ON proactive_log(considered_at DESC);


CREATE TABLE episodes (
    id              INTEGER PRIMARY KEY,
    correlation_id  TEXT    NOT NULL,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    turn_count      INTEGER NOT NULL DEFAULT 0,
    transcript      TEXT
);

CREATE INDEX idx_episodes_started ON episodes(started_at);   -- pruning (§7.5)
CREATE INDEX idx_episodes_corr    ON episodes(correlation_id);


-- FTS5 shadow of facts.text. §7.7's hybrid retrieval: vector search alone
-- cannot reliably retrieve "Maya" — proper nouns embed to mush.
CREATE VIRTUAL TABLE facts_fts USING fts5(
    text,
    content='facts',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER facts_fts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER facts_fts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER facts_fts_au AFTER UPDATE OF text ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;


CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  INTEGER NOT NULL,
    checksum    TEXT    NOT NULL
);
