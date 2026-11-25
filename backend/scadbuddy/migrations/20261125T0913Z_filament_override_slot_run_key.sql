-- #7276 Task 9 (P47): the key of an override slot's configure run that was still open when
-- the reconcile stopped waiting, so the next pass joins that run rather than sending the
-- commands again (`filament/rfid_slots.py`). NULL once the run's outcome is read.

ALTER TABLE filament_override_slots ADD COLUMN run_key text;
