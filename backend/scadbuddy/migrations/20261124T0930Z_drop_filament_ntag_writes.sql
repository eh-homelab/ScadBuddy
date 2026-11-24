-- #7273: the SpoolBuddy NTAG write (filament/ntag.py) is gone; tags are written from the
-- phone page with a backup first (filament_tag_backups).
DROP TABLE filament_ntag_writes;
