-- user.sqlite schema version 2: notes_for_ref / note_counts query note_targets by ref OR resolved_ref;
-- resolved_ref had no index, so every lookup scanned the table.
CREATE INDEX IF NOT EXISTS nt_resolved ON note_targets(resolved_ref);
