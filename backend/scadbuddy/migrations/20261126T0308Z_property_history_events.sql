-- #6949, inventory kinds and handlers design §4.8 (and §4.7, §6.3): the event stream is the
-- history. A trigger publishes each property set and remove, and each property schema
-- change, as an event in the writer's own transaction, so no writer can skip it. The
-- history subscriber in scadbuddy-core (tools/go/core/internal/history) reads the log from
-- its cursor in `event_subscribers` and inserts one history row per event, keyed by the
-- event's `seq`. No writer inserts a history row.
--
-- Each history event carries `retention_seconds: 0`, so the log's prune never removes it
-- (`core/pg_events.py` `prune_log`: an event's own retention comes first, and 0 keeps it).

-- The actor of the current transaction: `SELECT set_config('scadbuddy.actor', <Actor JSON>,
-- true)`. NULL when the transaction did not set it.
CREATE FUNCTION scadbuddy_actor() RETURNS jsonb
LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('scadbuddy.actor', true), '')::jsonb
$$;

-- The actor a history event names: refused unless it is a JSON object, as the history
-- subscriber stores only an object. A bad actor refuses the write, so it never reaches the
-- log, where it would stop the subscriber's cursor.
CREATE FUNCTION scadbuddy_event_actor(actor jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
  IF jsonb_typeof(actor) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION 'a history event''s actor must be a JSON object, not %', coalesce(actor::text, 'NULL')
      USING ERRCODE = 'P0001';
  END IF;
  RETURN actor;
END
$$;

-- The migration's own actor, for rows that name none.
CREATE FUNCTION scadbuddy_migration_actor() RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT '{"kind": "provider", "principal": "migration"}'::jsonb
$$;

-- Publish an event as `core/pg_events.py` and Go's `internal/bus` do: the event log's lock
-- (EVENT_LOG_LOCK, held to the writer's commit, so `seq` order is commit order), the row in
-- `events`, and the NOTIFY on `scadbuddy_events`, which Postgres sends only on commit.
-- `fields` follow id, at and kind. An event over 4000 bytes (`MAX_PAYLOAD_BYTES`, half the
-- NOTIFY cap) is logged whole; its NOTIFY leaves out `old` and `new` and says
-- `values_in_log`, so a listener reads the values from the log. Returns the event's `seq`.
CREATE FUNCTION scadbuddy_publish_event(kind text, fields jsonb) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  v_id text := replace(gen_random_uuid()::text, '-', '');
  v_at timestamptz;
  v_payload jsonb;
  v_seq bigint;
BEGIN
  PERFORM pg_advisory_xact_lock(5999710890064498260);  -- 0x5343414445564E54
  v_at := clock_timestamp();
  v_payload := jsonb_build_object('id', v_id, 'at', v_at, 'kind', scadbuddy_publish_event.kind)
    || fields;
  INSERT INTO events (event_id, kind, at, payload)
  VALUES (v_id, scadbuddy_publish_event.kind, v_at, v_payload)
  RETURNING seq INTO v_seq;
  IF octet_length(v_payload::text) <= 4000 THEN
    PERFORM pg_notify('scadbuddy_events', v_payload::text);
  ELSE
    PERFORM pg_notify('scadbuddy_events',
      (v_payload - 'old' - 'new' || '{"values_in_log": true}'::jsonb)::text);
  END IF;
  RETURN v_seq;
END
$$;

-- Each durable subscriber's place in the log: the last `seq` it applied.
CREATE TABLE event_subscribers (
  name text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_]{0,39}$'),
  seq bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- One row per `entity_property.changed` event, written only by the history subscriber.
-- No foreign keys: the log may be pruned of other kinds, and a row outlives its entity.
CREATE TABLE entity_property_history (
  event_seq bigint PRIMARY KEY,
  entity_id uuid NOT NULL,
  kind text NOT NULL,
  ref text NOT NULL,
  key text NOT NULL,
  op text NOT NULL CHECK (op IN ('set', 'remove')),
  old jsonb,
  new jsonb,
  source text,
  at timestamptz NOT NULL,
  actor jsonb NOT NULL
);
CREATE INDEX entity_property_history_entity ON entity_property_history (entity_id, key, event_seq);
CREATE INDEX entity_property_history_ref ON entity_property_history (kind, ref);

-- One row per `entity_property_schema.changed` event. `owner` is `kind:<entity kind>` or
-- `item_kind:<id>`.
CREATE TABLE entity_property_schema_history (
  event_seq bigint PRIMARY KEY,
  owner text NOT NULL,
  old jsonb,
  new jsonb NOT NULL,
  at timestamptz NOT NULL,
  actor jsonb NOT NULL
);
CREATE INDEX entity_property_schema_history_owner
  ON entity_property_schema_history (owner, event_seq);

-- The back-fill: the history starts after the log as it is now, with one `set` from nothing
-- per existing property and one event per existing schema, each at its row's `updated_at`
-- (`changed_at`). The lock keeps another publisher out until this commits.
SELECT pg_advisory_xact_lock(5999710890064498260);
INSERT INTO event_subscribers (name, seq) SELECT 'history', coalesce(max(seq), 0) FROM events;

DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT p.entity_id, e.kind, e.ref, p.key, p.value, p.source, p.updated_by, p.updated_at
    FROM entity_properties p JOIN entities e ON e.id = p.entity_id
    ORDER BY p.entity_id, p.key
  LOOP
    PERFORM scadbuddy_publish_event('entity_property.changed', jsonb_build_object(
      'entity_id', r.entity_id, 'entity_kind', r.kind, 'ref', r.ref, 'key', r.key,
      'op', 'set', 'old', NULL, 'new', r.value, 'source', r.source,
      'actor', CASE WHEN jsonb_typeof(r.updated_by) = 'object' THEN r.updated_by
        ELSE scadbuddy_migration_actor() END,
      'changed_at', r.updated_at, 'retention_seconds', 0));
  END LOOP;
  FOR r IN SELECT kind, schema, updated_by, updated_at FROM entity_property_schemas
    WHERE jsonb_typeof(schema) <> 'null' ORDER BY kind
  LOOP
    PERFORM scadbuddy_publish_event('entity_property_schema.changed', jsonb_build_object(
      'owner', 'kind:' || r.kind, 'old', NULL, 'new', r.schema,
      'actor', CASE WHEN jsonb_typeof(r.updated_by) = 'object' THEN r.updated_by
        ELSE scadbuddy_migration_actor() END,
      'changed_at', r.updated_at, 'retention_seconds', 0));
  END LOOP;
  FOR r IN SELECT id, attribute_schema, updated_at FROM item_kinds
    WHERE jsonb_typeof(attribute_schema) <> 'null' ORDER BY id
  LOOP
    PERFORM scadbuddy_publish_event('entity_property_schema.changed', jsonb_build_object(
      'owner', 'item_kind:' || r.id, 'old', NULL, 'new', r.attribute_schema,
      'actor', scadbuddy_migration_actor(),
      'changed_at', r.updated_at, 'retention_seconds', 0));
  END LOOP;
END
$$;

-- A property set or remove (R-K1-12). A set's actor is `scadbuddy.actor`, else the row's
-- `updated_by` (a merge copies `updated_by` from the old row, and is credited to who merged).
-- A remove needs `scadbuddy.actor`. A remove whose entity row is gone is a cascade, which
-- `entities_publish_property_removes` already published.
CREATE FUNCTION entity_properties_publish() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_actor jsonb := scadbuddy_actor();
  v_kind text;
  v_ref text;
  v_old jsonb;
BEGIN
  IF TG_OP = 'DELETE' THEN
    SELECT kind, ref INTO v_kind, v_ref FROM entities WHERE id = OLD.entity_id;
    IF NOT FOUND THEN
      RETURN NULL;
    END IF;
    IF v_actor IS NULL THEN
      RAISE EXCEPTION 'a property remove needs scadbuddy.actor' USING ERRCODE = 'P0001';
    END IF;
    PERFORM scadbuddy_publish_event('entity_property.changed', jsonb_build_object(
      'entity_id', OLD.entity_id, 'entity_kind', v_kind, 'ref', v_ref, 'key', OLD.key,
      'op', 'remove', 'old', OLD.value, 'new', NULL, 'source', NULL,
      'actor', scadbuddy_event_actor(v_actor),
      'retention_seconds', 0));
    RETURN NULL;
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF OLD.value IS NOT DISTINCT FROM NEW.value THEN
      RETURN NULL;
    END IF;
    v_old := OLD.value;
  END IF;
  SELECT kind, ref INTO v_kind, v_ref FROM entities WHERE id = NEW.entity_id;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  PERFORM scadbuddy_publish_event('entity_property.changed', jsonb_build_object(
    'entity_id', NEW.entity_id, 'entity_kind', v_kind, 'ref', v_ref, 'key', NEW.key,
    'op', 'set', 'old', v_old, 'new', NEW.value, 'source', NEW.source,
    'actor', scadbuddy_event_actor(coalesce(v_actor, NEW.updated_by)), 'retention_seconds', 0));
  RETURN NULL;
END
$$;

CREATE TRIGGER entity_properties_publish
AFTER INSERT OR UPDATE OR DELETE ON entity_properties
FOR EACH ROW EXECUTE FUNCTION entity_properties_publish();

-- An entity's delete (the orphan sweep, a merge) cascades to its properties after its row
-- is gone: one remove per property is published first, with the entity's kind and ref.
CREATE FUNCTION entities_publish_property_removes() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_actor jsonb := scadbuddy_actor();
  p record;
BEGIN
  FOR p IN SELECT key, value FROM entity_properties WHERE entity_id = OLD.id ORDER BY key
  LOOP
    IF v_actor IS NULL THEN
      RAISE EXCEPTION 'a property remove needs scadbuddy.actor' USING ERRCODE = 'P0001';
    END IF;
    PERFORM scadbuddy_publish_event('entity_property.changed', jsonb_build_object(
      'entity_id', OLD.id, 'entity_kind', OLD.kind, 'ref', OLD.ref, 'key', p.key,
      'op', 'remove', 'old', p.value, 'new', NULL, 'source', NULL,
      'actor', scadbuddy_event_actor(v_actor),
      'retention_seconds', 0));
  END LOOP;
  RETURN OLD;
END
$$;

CREATE TRIGGER entities_publish_property_removes
BEFORE DELETE ON entities
FOR EACH ROW EXECUTE FUNCTION entities_publish_property_removes();

-- A kind's property schema (`entity_property_schemas`).
CREATE FUNCTION entity_property_schemas_publish() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_old jsonb;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF OLD.schema IS NOT DISTINCT FROM NEW.schema THEN
      RETURN NULL;
    END IF;
    v_old := OLD.schema;
  END IF;
  IF jsonb_typeof(NEW.schema) = 'null' THEN
    RAISE EXCEPTION 'a property schema must not be JSON null' USING ERRCODE = 'P0001';
  END IF;
  PERFORM scadbuddy_publish_event('entity_property_schema.changed', jsonb_build_object(
    'owner', 'kind:' || NEW.kind, 'old', v_old, 'new', NEW.schema,
    'actor', scadbuddy_event_actor(
      coalesce(scadbuddy_actor(), NEW.updated_by, scadbuddy_migration_actor())),
    'retention_seconds', 0));
  RETURN NULL;
END
$$;

CREATE TRIGGER entity_property_schemas_publish
AFTER INSERT OR UPDATE ON entity_property_schemas
FOR EACH ROW EXECUTE FUNCTION entity_property_schemas_publish();

-- An item kind's `attribute_schema`. A function of its own: `item_kinds` has no
-- `updated_by`, and PL/pgSQL fails on a field the row does not have.
CREATE FUNCTION item_kinds_publish_schema() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_old jsonb;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF OLD.attribute_schema IS NOT DISTINCT FROM NEW.attribute_schema THEN
      RETURN NULL;
    END IF;
    v_old := OLD.attribute_schema;
  END IF;
  IF jsonb_typeof(NEW.attribute_schema) = 'null' THEN
    RAISE EXCEPTION 'an item kind''s attribute_schema must not be JSON null' USING ERRCODE = 'P0001';
  END IF;
  PERFORM scadbuddy_publish_event('entity_property_schema.changed', jsonb_build_object(
    'owner', 'item_kind:' || NEW.id, 'old', v_old, 'new', NEW.attribute_schema,
    'actor', scadbuddy_event_actor(coalesce(scadbuddy_actor(), scadbuddy_migration_actor())),
    'retention_seconds', 0));
  RETURN NULL;
END
$$;

CREATE TRIGGER item_kinds_publish_schema
AFTER INSERT OR UPDATE OF attribute_schema ON item_kinds
FOR EACH ROW EXECUTE FUNCTION item_kinds_publish_schema();
