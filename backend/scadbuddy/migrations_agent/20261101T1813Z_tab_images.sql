-- #254: the images a ScadBuddy tab answers browser_screenshot with, between the
-- replica holding the tab's bridge socket and the one whose call asked for it
-- (bridge/tabImages.ts). The tab sends the bytes as binary frames to its replica,
-- which names them by their sha256 and stores them here; the call's answer names
-- the image only (#2479). The tool's read deletes the row; one never read is
-- dropped after ten minutes, by the next write or read.
CREATE TABLE ai_tab_images (
  name        text PRIMARY KEY CHECK (name ~ '^[0-9a-f]{64}\.(png|jpg|gif|webp)$'),
  media_type  text NOT NULL CHECK (media_type IN ('image/png', 'image/jpeg', 'image/gif', 'image/webp')),
  bytes       bytea NOT NULL,
  stored_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_tab_images_stored_at ON ai_tab_images (stored_at);
