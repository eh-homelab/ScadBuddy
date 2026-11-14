-- Unified runtime plan Phase 10 (#2726): two settings nothing reads. `session_mode` was the
-- classic/durable switch, unread since plan Step 6.3. `durable_subagents` was agent-durable's
-- route to the agent's loopback subagent MCP, unread since Step 8.2 removed that route. Neither
-- is named by the build running before this one (sha-c71c7cd), so the delete is safe in a
-- rolling deploy. The ai_sessions columns those runtimes used are dropped in a later release,
-- once this one, which reads none of them, runs everywhere.
DELETE FROM ai_settings WHERE key IN ('session_mode', 'durable_subagents');
