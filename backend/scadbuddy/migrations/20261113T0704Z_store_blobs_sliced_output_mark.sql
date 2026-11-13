-- #5298 Task 32: the print worker's read URL is minted only for a slicer's output, which
-- `Transfers.finish_put` now marks in `store_blobs.meta` (`slicing/transfer.py`
-- `SLICED_OUTPUT_META`). The slicer's inputs share kind `slice` and stay unmarked. Mark the
-- outputs stored before the mark that a native slice or a Bambuddy upload's slice still
-- names, so a slice reused after the deploy still prints over the LAN.
-- Accepted gap: a LAN run of several plates or copies whose `native_slices` row was swept
-- (6 h after it finished) before this runs keeps its key only in workflow state, so its
-- next plate gets one 410 "Print again to slice it again", once, at the deploy.
-- Accepted gap (rolling deploy): an API pod still on the old build finishes slices
-- without the mark, after this ran, so a LAN run of such a slice gets one 410, once.
-- Accepted (credentials stripped at parse, same change): a local preset that carried a
-- print-host credential key gets a new document digest, so its slices key apart once
-- (one reslice), and across a rolling deploy an old print worker's start may be refused
-- once as "A preset Bambuddy holds changed".
UPDATE store_blobs SET meta = meta || '{"output": "sliced"}'::jsonb
WHERE kind = 'slice'
  AND key IN (
    SELECT sliced_blob_key FROM native_slices WHERE sliced_blob_key IS NOT NULL
    UNION
    SELECT sliced_blob_key FROM output_bambuddy_slices WHERE sliced_blob_key IS NOT NULL
  );
