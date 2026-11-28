-- #8190: a save that changes only a property's `source` (a `manual` save that keeps a
-- provider's value) is an event too, so the history shows who owns the value. The rest of
-- `entity_properties_publish` is as 20261126T0308Z_property_history_events.sql made it.
CREATE OR REPLACE FUNCTION entity_properties_publish() RETURNS trigger
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
    IF (OLD.value, OLD.source) IS NOT DISTINCT FROM (NEW.value, NEW.source) THEN
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
