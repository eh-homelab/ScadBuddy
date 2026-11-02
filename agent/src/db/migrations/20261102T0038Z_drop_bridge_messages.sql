-- #5109: the Postgres browser relay's table. #4868 (PR #5126) removed the relay, and every
-- agent replica now runs that build, so nothing writes or reads it.
DROP TABLE IF EXISTS ai_bridge_messages;
