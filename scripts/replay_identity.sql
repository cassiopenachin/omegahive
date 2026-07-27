-- Ordered-event replay-identity checksum for one run, bounded by a sequence number.
--
-- The restore drill's core assertion (deployment spec §7 check 3; drilled against live
-- content 2026-07-13): a dump restored elsewhere replays byte-identical to its source.
-- Comparing a restored copy against a LIVE spine needs the bound, because the live log
-- keeps growing after the dump is taken — so both sides checksum only `seq <= :maxseq`,
-- where :maxseq is the newest sequence the restored copy holds.
--
-- Lives in scripts/ because the compose `backup` service already mounts ./scripts at
-- /scripts:ro,Z — so the identical query runs against the live store and against a
-- scratch stack with no compose change on either side.
--
-- Usage (inside the pinned postgres image, via the ops-profile backup service):
--   psql "$OMEGAHIVE_DATABASE_URL" -v ON_ERROR_STOP=1 -tA \
--     -v run=omegahive -v maxseq=15528 -f /scripts/replay_identity.sql
--
-- Output: one pipe-separated line -- <count>|<maxseq>|<md5>
--
-- Determinism notes: wall_ts is rendered through an explicit `AT TIME ZONE 'UTC'` +
-- to_char rather than a session `SET TIME ZONE`, so the digest cannot depend on the
-- caller's timezone AND the file stays a single statement (a `SET` would emit its own
-- status line and corrupt the -tA output the callers parse); jsonb has a canonical text
-- form, so payload::text is stable across dump/restore; every nullable column is coalesced
-- so a NULL and an empty string can never collide into the same digest.

SELECT count(*)
       || '|' || coalesce(max(seq), 0)
       || '|' || coalesce(md5(string_agg(row_text, E'\n' ORDER BY seq)), 'empty')
FROM (
    SELECT seq,
           concat_ws('|',
               seq,
               event_id,
               run_id,
               logical_ts,
               coalesce(to_char(wall_ts AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US'), ''),
               actor_role,
               actor_id,
               event_type,
               coalesce(task_id, ''),
               payload::text,
               coalesce(causation_id::text, ''),
               correlation_id,
               coalesce(recipient_role, ''),
               coalesce(recipient_id, '')
           ) AS row_text
    FROM events
    WHERE run_id = :'run'
      AND seq <= :maxseq
) ordered;
