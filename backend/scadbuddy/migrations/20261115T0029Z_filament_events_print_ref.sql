-- The native settle's lookup of a run's print estimates (#6261, `spools/native.py`
-- `settle_uses_in`, `record_uses`): `ref_id` is `run/subtask/slot`, found by the run's
-- prefix (`LIKE 'run/%'`) or whole. `text_pattern_ops` serves the prefix match under
-- any collation.
CREATE INDEX filament_spool_events_print_ref
    ON filament_spool_events (ref_kind, ref_id text_pattern_ops)
    WHERE kind = 'print_started';
