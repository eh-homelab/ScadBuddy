-- #6233: when the API read a row's object and found the row's SHA-256 and size
-- (`api/crud_render.py` `_verified`). A render write of a ref skips that read only when a
-- row it verified names the same object. A write that points the row at another object
-- clears it (`store/index.py`), and so does the switch to a migrated copy. Rows written
-- before this have none, so the next render write that reuses one reads it once.
ALTER TABLE store_blobs ADD COLUMN verified_at timestamptz;
