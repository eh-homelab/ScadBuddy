-- `GET /filament/events` with no spool filter pages `filament_spool_events` by
-- `at DESC, seq DESC` (`filament/ledger.py` `page`); this index serves that order
-- (final review of #6262, M-6).
CREATE INDEX filament_spool_events_at_seq ON filament_spool_events (at DESC, seq DESC);
