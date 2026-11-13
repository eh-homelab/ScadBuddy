-- #5298 Task 32: the print worker's read URL is minted only for a slicer's output, which
-- the internal PUT route now marks in `store_blobs.meta` (`slicing/transfer.py`
-- `SLICED_OUTPUT_META`). The slicer's inputs share kind `slice` and stay unmarked. Mark the
-- outputs stored before the mark that a native slice or a Bambuddy upload's slice still
-- names, so a slice reused after the deploy still prints over the LAN.
UPDATE store_blobs SET meta = meta || '{"output": "sliced"}'::jsonb
WHERE kind = 'slice'
  AND key IN (
    SELECT sliced_blob_key FROM native_slices WHERE sliced_blob_key IS NOT NULL
    UNION
    SELECT sliced_blob_key FROM output_bambuddy_slices WHERE sliced_blob_key IS NOT NULL
  );
