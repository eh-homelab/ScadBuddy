-- #815 §3: Web Push, so a closed ScadBuddy tab still alerts (notify/pushStore.ts).

-- The VAPID key pair (RFC 8292): one per deployment, made on first use. The private key is
-- sealed under the KEK (secrets.ts, AAD `push-vapid:1`); never an env var.
CREATE TABLE ai_push_vapid (
  id            smallint PRIMARY KEY CHECK (id = 1),
  -- The uncompressed P-256 point the browser subscribes with (`applicationServerKey`).
  public_key    bytea NOT NULL CHECK (length(public_key) = 65),
  secret_sealed bytea NOT NULL,
  dek_sealed    bytea NOT NULL,
  kek_id        text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- A browser's push subscription, for the person who made it. Its `p256dh` and `auth` keys
-- are sealed with the endpoint in the AAD (`push-subscription:<id>:<endpoint>`).
CREATE TABLE ai_push_subscriptions (
  id            uuid PRIMARY KEY,
  owner_kind    text NOT NULL,
  owner_id      text NOT NULL,
  owner_label   text NOT NULL,
  endpoint      text NOT NULL UNIQUE CHECK (length(endpoint) <= 2048),
  secret_sealed bytea NOT NULL,
  dek_sealed    bytea NOT NULL,
  kek_id        text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Which request each target has been told of (`push:<subscription id>`), so replicas hearing
-- one change send it once. Kept two days, past any entry's 86 400 s ceiling.
CREATE TABLE ai_notifications_sent (
  request_id text NOT NULL,
  target     text NOT NULL,
  sent_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (request_id, target)
);
CREATE INDEX ai_notifications_sent_at ON ai_notifications_sent (sent_at);
