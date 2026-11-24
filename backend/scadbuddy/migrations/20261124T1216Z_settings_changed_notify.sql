-- #7384: every write to `settings` NOTIFYs `scadbuddy_settings` at its commit, so each
-- API replica's cached plain settings (`library/settings_store.py` `SettingsCache`) are
-- dropped whoever wrote the row: the settings store, the printer registry's merge and
-- delete (`printers/store.py`), a migration or a hand-run fix (`store/backends.py`
-- `RECOVER_LOCAL_SQL`). Once per statement; Postgres folds a transaction's identical
-- notifications into one. The payload is empty: a listener drops its whole cache.
CREATE FUNCTION settings_changed_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('scadbuddy_settings', '');
  RETURN NULL;
END;
$$;

CREATE TRIGGER settings_changed_notify
  AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON settings
  FOR EACH STATEMENT EXECUTE FUNCTION settings_changed_notify();
