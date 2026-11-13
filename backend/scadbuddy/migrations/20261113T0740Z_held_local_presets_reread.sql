-- #5298 Task 32: a local preset's `setting` now loses its print-host credential keys when
-- it is parsed (`bambuddy/models.py` `LocalPresetDetail`, `core/credentials.py`
-- `strip_credentials`), so a refresh keeps it without them. A row kept by an older build
-- may still hold them. Clear every row's stamp, so the next refresh reads each preset
-- again and rewrites it stripped (`slicing/held_presets.py` `refresh_held_presets` reads a
-- preset whose stamp differs from its list row's). Until then each read of a row strips
-- it at parse. A row with no such key is rewritten with the same document and `sha256`.
UPDATE held_local_presets SET updated_at = NULL;
