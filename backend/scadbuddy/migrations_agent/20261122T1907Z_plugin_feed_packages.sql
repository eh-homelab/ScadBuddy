-- #6933 (spec 2026-11-21 §6.1, §6.4): which packages a marketplace feed lists. The agent
-- serves the feed (`/api/v1/ai/plugin-feeds/{feed}/marketplace.json`) from these rows,
-- each package at its current approved pin, read through the API's CRUD route
-- (backend api/crud_agent_packages.py, area `plugin-feeds`). The routes that add and remove
-- a package are #6936's. Deleting a package, or the feed, removes its rows.
CREATE TABLE ai_plugin_feed_packages (
  feed_id      bigint NOT NULL REFERENCES ai_plugin_feeds (id) ON DELETE CASCADE,
  package_name text NOT NULL REFERENCES ai_plugin_packages (name) ON DELETE CASCADE,
  added_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (feed_id, package_name)
);
-- The FK's other side: a package's delete finds its rows without a scan.
CREATE INDEX ai_plugin_feed_packages_package ON ai_plugin_feed_packages (package_name);
