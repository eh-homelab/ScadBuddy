-- #2171: a listing that is not every tool the source has (a nextCursor, or entries the caps
-- dropped: nameless, a name over 128 characters, past 512) says so. `tools` stays an array.
ALTER TABLE ai_tool_listings ADD COLUMN truncated boolean NOT NULL DEFAULT false;
