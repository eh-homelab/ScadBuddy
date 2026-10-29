-- #4796: whether the LAN link still follows Bambuddy's address after a move to it failed,
-- so the next sync tries the move again. It was read off `lan_sync_error`'s wording,
-- which a reworded message (or a sync that cleared it) silently ended. Any LAN link
-- made or removed clears it. Rows already holding that message are pending moves.
ALTER TABLE printers ADD COLUMN lan_move_pending boolean NOT NULL DEFAULT false;

UPDATE printers SET lan_move_pending = true
WHERE lan_host IS NOT NULL
    AND lan_sync_error = 'Bambuddy reports a new address and the printer did not answer there;'
        ' the LAN link keeps the old one, and the next sync tries again';
