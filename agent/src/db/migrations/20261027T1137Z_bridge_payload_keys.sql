-- #4780: every BridgeCall workflow (a browser_* call to the replica holding the tab,
-- src/bridge/relay.ts) is a subject of its own, `bridge-<uuid>`, so its payloads (page
-- snapshots, form contents, the editor's text) are sealed in Temporal history even when
-- the call belongs to no session. The caller deletes the call's key once it has the
-- answer, which crypto-shreds that history; a key a cancelled or lost call left is
-- deleted by the hourly sweep (payloadCodec.ts `sweepBridgeKeys`).
ALTER TABLE ai_payload_keys DROP CONSTRAINT ai_payload_keys_subject_check;
ALTER TABLE ai_payload_keys ADD CONSTRAINT ai_payload_keys_subject_check
  CHECK (subject ~ '^(session|flow|bridge)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$');
