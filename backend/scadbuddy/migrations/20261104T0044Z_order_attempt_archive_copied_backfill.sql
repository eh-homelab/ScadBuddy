-- #5276: 20261103T2309Z's backfill marked copied only an attempt that had copied seconds or
-- grams, or had a run. One named by hand whose archive gives neither was copied all the
-- same (its times written), and its first read after that file copied the times again over
-- any correction since. Nothing tells such an attempt from one never read, so every attempt
-- that named archives before that file is taken as copied: a correction is never put back,
-- and one never read keeps the times it was recorded with (its seconds and grams are still
-- copied on every read). An attempt recorded since that file is left for its first copy.
UPDATE order_item_fulfilments SET archive_copied = true
 WHERE NOT archive_copied
   AND archive_ids <> '{}'
   AND created_at < (
     SELECT applied_at FROM scadbuddy_migrations
      WHERE id = '20261103T2309Z_order_attempt_archive_copied'
   );
