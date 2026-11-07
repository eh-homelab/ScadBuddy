-- Each status an order or a line item moves to (#4252, orders spec §7.1), one row per
-- change, kept by the tables' own trigger, so no writer can forget it; only the API
-- writes these tables. An order trigger fires once per row (its fire key names the row):
-- a plain edit, a hold or a release writes none, so it fires nothing.
CREATE TABLE order_status_changes (
  id bigserial PRIMARY KEY,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  -- NULL for the order's own status.
  item_id text,
  status text NOT NULL,
  -- When the row was written, not when its transaction began (`now()`): a trigger
  -- made while the change's transaction was open still hears it.
  at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX order_status_changes_subject ON order_status_changes (order_id, item_id, id);

CREATE FUNCTION order_status_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status THEN
    IF TG_TABLE_NAME = 'orders' THEN
      INSERT INTO order_status_changes (order_id, item_id, status) VALUES (NEW.id, NULL, NEW.status);
    ELSE
      INSERT INTO order_status_changes (order_id, item_id, status) VALUES (NEW.order_id, NEW.id, NEW.status);
    END IF;
  END IF;
  RETURN NULL;
END
$$;

CREATE TRIGGER orders_status_changed AFTER INSERT OR UPDATE OF status ON orders
  FOR EACH ROW EXECUTE FUNCTION order_status_changed();
CREATE TRIGGER order_items_status_changed AFTER INSERT OR UPDATE OF status ON order_items
  FOR EACH ROW EXECUTE FUNCTION order_status_changed();

-- Since when a trigger has listened (made, last edited, or last resumed): a change
-- before it is not one it heard, so it never fires for it. Both clocks are this
-- database's.
ALTER TABLE flow_triggers ADD COLUMN live_since timestamptz NOT NULL DEFAULT now();
