-- #8366: what the ScadBuddy app last read of a tag's chip (#8362): the NTAG chip from
-- GET_VERSION (NXP NTAG213/215/216 data sheet), Android's Ndef.getMaxSize() and isWritable(),
-- and when. NULL when no read has told them (a Web NFC read, an older app, an AMS report).
ALTER TABLE filament_tags
    ADD COLUMN chip text CHECK (char_length(chip) <= 32),
    ADD COLUMN max_ndef_bytes integer CHECK (max_ndef_bytes BETWEEN 0 AND 65535),
    ADD COLUMN writable boolean,
    ADD COLUMN chip_read_at timestamptz;
