-- #780: OAuth 2.1 sign-in for remote MCP plugins (src/plugins/oauth.ts, oauthStore.ts).
-- A person signs a plugin in from Settings; the agent holds the tokens, sealed under the
-- KEK, and the plugin forwarder adds a fresh access token to each request. Claude Code
-- never sees one.

-- One grant per plugin: what the token endpoint gave the last sign-in.
CREATE TABLE ai_plugin_oauth (
  plugin          text PRIMARY KEY REFERENCES ai_plugins (name) ON DELETE CASCADE,
  -- The plugin's endpoint when it was signed in: a URL change drops the grant (registry.ts).
  plugin_url      text NOT NULL,
  -- The canonical resource sent as RFC 8707 `resource`, from the protected-resource metadata.
  resource        text NOT NULL,
  -- The authorization server's validated `issuer` (RFC 8414 §3.3), and its token endpoint.
  issuer          text NOT NULL,
  token_endpoint  text NOT NULL,
  client_id       text NOT NULL,
  -- How the agent authenticates at the token endpoint (RFC 8414 token_endpoint_auth_methods).
  auth_method     text NOT NULL CHECK (auth_method IN ('none', 'client_secret_post', 'client_secret_basic')),
  -- Where the client id came from: RFC 7591 registration, or the person's own OAuth app.
  registration    text NOT NULL CHECK (registration IN ('dynamic', 'configured')),
  scope           text,
  -- {access_token, refresh_token?, client_secret?} sealed (src/secrets.ts); the AAD binds it
  -- to the plugin, its URL, the resource, the issuer, the token endpoint and the client id.
  secret_sealed   bytea NOT NULL,
  dek_sealed      bytea NOT NULL,
  kek_id          text NOT NULL,
  has_refresh     boolean NOT NULL,
  -- NULL: the server gave no expires_in.
  access_expires_at timestamptz,
  -- `error`: a refresh was refused or the server refused the token; the person signs in again.
  status          text NOT NULL DEFAULT 'connected' CHECK (status IN ('connected', 'error')),
  last_error      text,
  -- New at each sign-in: what a warm process's fingerprint names in place of the token.
  grant_id        uuid NOT NULL,
  connected_by    text NOT NULL,
  connected_at    timestamptz NOT NULL DEFAULT now(),
  refreshed_at    timestamptz
);

-- A sign-in under way: single use, short-lived. Rows past `expires_at` are deleted by the
-- next start.
CREATE TABLE ai_plugin_oauth_flows (
  -- sha256 of the `state` sent to the authorization server; the state itself is sealed.
  state_hash      text PRIMARY KEY,
  plugin          text NOT NULL REFERENCES ai_plugins (name) ON DELETE CASCADE,
  plugin_url      text NOT NULL,
  -- sha256 of the one-use link that opens the flow in the person's browser; NULL once spent.
  ticket_hash     text UNIQUE,
  -- sha256 of the browser cookie set when the link was opened; the callback must carry it.
  binding_hash    text,
  -- issuer, endpoints, resource, scope, client id, auth method, registration, redirect URI.
  meta            jsonb NOT NULL,
  -- {state, verifier, client_secret?} sealed; the AAD binds it to state_hash, plugin and URL.
  secret_sealed   bytea NOT NULL,
  dek_sealed      bytea NOT NULL,
  kek_id          text NOT NULL,
  created_by      text NOT NULL,
  expires_at      timestamptz NOT NULL
);
CREATE INDEX ai_plugin_oauth_flows_expires ON ai_plugin_oauth_flows (expires_at);
