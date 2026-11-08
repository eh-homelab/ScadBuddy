-- #5282 (storefront phase 3, spec 2026-11-02 §5.3): what the public site offers. A listing
-- names one commit of a template, the parameters a customer may change (`params`, each
-- with its bounds) and the values of the rest (`fixed`), the price-book filaments a
-- customer may pick (`colours`) and the template media to show (`media_ids`).
-- Publish captures what a visitor's render needs without git (`snapshot`) and copies
-- the media into the content store (`media`); both blobs are held in `blob_refs` under
-- ('storefront_listing', id) while the listing is published.
CREATE TABLE storefront_listings (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  slug text NOT NULL,
  commit text NOT NULL CHECK (commit ~ '^[0-9a-f]{40}$'),
  title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
  description text NOT NULL DEFAULT '' CHECK (char_length(description) <= 5000),
  position integer NOT NULL,
  published boolean NOT NULL DEFAULT false,
  params jsonb NOT NULL DEFAULT '{}',
  fixed jsonb NOT NULL DEFAULT '{}',
  colours text[] NOT NULL DEFAULT '{}',
  media_ids text[] NOT NULL DEFAULT '{}',
  snapshot jsonb,
  media jsonb NOT NULL DEFAULT '[]',
  published_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (NOT published OR snapshot IS NOT NULL)
);

CREATE INDEX storefront_listings_published ON storefront_listings (published, position);
