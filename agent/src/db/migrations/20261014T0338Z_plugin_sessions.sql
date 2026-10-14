CREATE TABLE ai_plugin_sessions (
  plugin_id text NOT NULL,
  upstream_session_id text NOT NULL,
  replica text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (plugin_id, upstream_session_id)
);
