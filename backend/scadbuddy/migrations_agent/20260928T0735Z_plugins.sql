-- #297: the plugin registry (spec §9, "plugins: ... endpoint credentials
-- (encrypted), and tier map"). Remote MCP endpoints only; plugin packages
-- would widen `kind` in a later file. See src/plugins/registry.ts.
      CREATE TABLE ai_plugins (
        -- The MCP server name the harness registers, so its tools are
        -- mcp__<name>__<tool>. Lower-case letters, digits and single hyphens:
        -- no underscore, so no name can forge the "__" separator.
        name           text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9-]{0,30}[a-z0-9]$' AND name !~ '--'),
        kind           text NOT NULL CHECK (kind IN ('remote_mcp')),
        -- Streamable HTTP endpoint (spec D5). https, or http to loopback only.
        url            text NOT NULL,
        -- Off until an admin has reviewed the plugin's tools (issue #297,
        -- "Review before enable").
        enabled        boolean NOT NULL DEFAULT false,
        -- Optional auth header: its name in the clear, its value sealed
        -- (src/secrets.ts; AAD binds it to name, url and header name).
        auth_header    text,
        secret_sealed  bytea,
        dek_sealed     bytea,
        kek_id         text,
        last4          text,
        -- Explicit per-tool tiers, { "<tool>": "read" | "write" | "outward" };
        -- a tool not listed is outward (spec §8.1).
        tool_tiers     jsonb NOT NULL DEFAULT '{}',
        -- Tools removed from the model's view (SDK disallowedTools).
        disabled_tools text[] NOT NULL DEFAULT '{}',
        created_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now(),
        CHECK (jsonb_typeof(tool_tiers) = 'object'),
        CHECK ((secret_sealed IS NULL) = (dek_sealed IS NULL)),
        CHECK ((secret_sealed IS NULL) = (kek_id IS NULL)),
        CHECK ((secret_sealed IS NULL) = (last4 IS NULL)),
        CHECK ((secret_sealed IS NULL) = (auth_header IS NULL))
      );
    