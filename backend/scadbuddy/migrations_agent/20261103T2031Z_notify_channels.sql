-- #815 §3: notification channels out of the browser (notify/channels.ts), set in Settings
-- (where the agent cannot write): an ntfy topic, or a Home Assistant notify service. Each
-- is told of every attention request once (ai_notifications_sent, target `channel:<id>`).
-- A token is sealed under the KEK with the channel's id, kind and URL in the AAD
-- (`notify-channel:<id>:<kind>:<url>`), so it is only ever sent where it was entered for.
CREATE TABLE ai_notify_channels (
  id            uuid PRIMARY KEY,
  kind          text NOT NULL CHECK (kind IN ('ntfy', 'home_assistant')),
  name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  url           text NOT NULL CHECK (length(url) <= 2048),
  -- Home Assistant's notify service (`mobile_app_<device>`); null for ntfy.
  target        text,
  enabled       boolean NOT NULL DEFAULT true,
  secret_sealed bytea,
  dek_sealed    bytea,
  kek_id        text,
  token_last4   text NOT NULL DEFAULT '',
  last_error    text,
  last_sent_at  timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK ((secret_sealed IS NULL) = (dek_sealed IS NULL) AND (dek_sealed IS NULL) = (kek_id IS NULL)),
  CHECK ((kind = 'home_assistant') = (target IS NOT NULL))
);
