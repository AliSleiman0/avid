-- ─────────────────────────────────────────────────────────────
-- 0002_runtime.sql — the boot log (#379, SDS §12.6)
--
-- O5 is "30-day soak, >=99% uptime, zero manual restarts" and NOTHING in this
-- system counted either quantity. `lifecycle.run` minted a boot_id and logged
-- one line; that was the whole record a run left behind. So the milestone's
-- headline criterion could only be answered by hand-reading a month of
-- journalctl, which is an anecdote with a large denominator, not a measurement.
--
-- Lives in the same file as `facts` / `routines` / `proactive_log` because
-- `proactive_log` is the precedent: an operational audit table, in the robot's
-- one database, read afterwards with plain SQL. The M11 grader queries this the
-- same way M10's AC-0 evidence is collected.
-- ─────────────────────────────────────────────────────────────

CREATE TABLE boot_log (
    id            INTEGER PRIMARY KEY,

    boot_id       TEXT    NOT NULL,   -- the lifecycle's correlation id for this boot
    build         TEXT    NOT NULL,   -- from the #373 banner; a window whose build
                                      -- changes mid-flight is not one window (§12.6)

    -- Wall clock, epoch seconds. Spanning a restart is the one thing a monotonic
    -- clock cannot do — there is no shared monotonic origin across two processes.
    started_at    INTEGER NOT NULL,

    -- This process's own monotonic reading at start. Kept so a CLOCK STEP is
    -- detectable after the fact rather than inferred from something that
    -- overslept: the Pi has no RTC, an offline boot restores a stale clock, and
    -- NTP steps it forward later. That is AVID-345 (~34,700 s of missed booking).
    started_mono  INTEGER NOT NULL,

    -- Refreshed on a heartbeat. On a run that died without warning this is the
    -- last moment the process is KNOWN to have been alive, so downtime is bounded
    -- by the heartbeat interval instead of guessed.
    last_seen_at  INTEGER NOT NULL,

    -- NULL = the process never got to say goodbye: crash, watchdog kill, power
    -- cut. That is precisely what makes a restart UNPLANNED (§12.6). A clean stop
    -- means the ordered teardown ran, which only happens when someone asked.
    stopped_at    INTEGER,
    stop_reason   TEXT CHECK (stop_reason IS NULL OR stop_reason IN ('signal')),

    -- The two halves of "did it stop" must agree, or a row means two things at
    -- once. Same shape as `facts`'s superseded_by/superseded_at pairing.
    CHECK ((stopped_at IS NULL) = (stop_reason IS NULL))
);

-- The grader's only access pattern: every run overlapping a window, in order.
-- Windows are measured in days, so this is the difference between a scan of a
-- month of rows and a scan of every boot since the robot was built.
CREATE INDEX idx_boot_log_started ON boot_log(started_at);
