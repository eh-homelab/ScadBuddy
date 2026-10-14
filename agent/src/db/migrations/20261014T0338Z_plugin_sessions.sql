-- The upstream MCP sessions this service's forwarders hold open for a warm Claude Code
-- process, by replica, so a replica that dies without ending them is swept by the others
-- (plugins/pluginSessions.ts). No secret is stored: the sweep reads the plugin's URL and
-- header from ai_plugins by plugin_id.
-- Limitation: a package's http server is not in ai_plugins, so after a crash the sweep drops
-- its rows without a DELETE (and logs the name); its session expires on the server's own timer.
CREATE TABLE ai_plugin_sessions (
  plugin_id text NOT NULL,
  upstream_session_id text NOT NULL,
  replica text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (plugin_id, upstream_session_id)
);
