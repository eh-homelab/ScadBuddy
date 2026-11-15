
      -- One row per credential; the service uses id 'default' (src/credentials.ts).
      -- kind 'anthropic_api_key' reaches the SDK as ANTHROPIC_API_KEY; 'gateway'
      -- as ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN (src/harness/run.ts).
      CREATE TABLE ai_credentials (
        id            text PRIMARY KEY,
        kind          text NOT NULL CHECK (kind IN ('anthropic_api_key', 'gateway')),
        base_url      text,
        secret_sealed bytea NOT NULL,
        dek_sealed    bytea NOT NULL,
        kek_id        text NOT NULL,
        last4         text NOT NULL,
        created_at    timestamptz NOT NULL DEFAULT now(),
        updated_at    timestamptz NOT NULL DEFAULT now(),
        CHECK ((kind = 'gateway') = (base_url IS NOT NULL))
      );

      -- Non-secret AI settings (model, budget caps, ...), one JSON value per key.
      -- Never put a secret here: values are returned to Settings as they are.
      CREATE TABLE ai_settings (
        key        text PRIMARY KEY,
        value      jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
      );
    