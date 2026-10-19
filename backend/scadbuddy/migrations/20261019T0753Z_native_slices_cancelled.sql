-- A cancelled native slice is recorded as `cancelled`, not as a failure (Task 11 review M4).
ALTER TABLE native_slices DROP CONSTRAINT native_slices_status_check;
ALTER TABLE native_slices ADD CONSTRAINT native_slices_status_check
  CHECK (status IN ('running', 'completed', 'failed', 'cancelled'));
