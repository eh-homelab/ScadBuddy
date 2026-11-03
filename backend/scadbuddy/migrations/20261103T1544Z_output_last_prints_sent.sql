-- #5143: whether a print of the output ever reached Bambuddy's queue. A LAN print clears
-- the row's Bambuddy ids (`library/output_prints.py` `record_lan`), as the last print is
-- no longer theirs, so this keeps what they said: an output queued to Bambuddy, then
-- printed over the LAN, still says "Send again". A Bambuddy print's row sets it; a LAN
-- print's keeps it. A LAN row written before this lost the ids already, so it stays false.
ALTER TABLE output_last_prints
    ADD COLUMN sent_to_bambuddy boolean NOT NULL DEFAULT false;

UPDATE output_last_prints SET sent_to_bambuddy = true WHERE print_route <> 'lan';
