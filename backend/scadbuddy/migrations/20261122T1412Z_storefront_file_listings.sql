-- #6710 (spec 2026-11-21 §4.3): a listing sells a commit of a template (`source`
-- 'template', as every listing before) or a library file ('library_file'). A file
-- listing names no slug or commit; `library_file` holds {id, library, pinned, repin,
-- filename}: `pinned` is the sha256 of the held copy (`listing-file-<sha256>`, held in
-- `blob_refs` under ('storefront_listing_kept', id)) the listing sells, null before its
-- first publish; `repin` asks the next publish to read the library's file again.
ALTER TABLE storefront_listings
  ADD COLUMN source text NOT NULL DEFAULT 'template'
    CHECK (source IN ('template', 'library_file')),
  ADD COLUMN library_file jsonb,
  ALTER COLUMN slug DROP NOT NULL,
  ALTER COLUMN commit DROP NOT NULL;

ALTER TABLE storefront_listings ADD CONSTRAINT storefront_listings_source CHECK (
  CASE source
    WHEN 'template' THEN slug IS NOT NULL AND commit IS NOT NULL AND library_file IS NULL
    ELSE slug IS NULL AND commit IS NULL AND library_file IS NOT NULL
  END
);

-- A storefront line of a file listing (§10.1): the listing's pinned sha256 and its held
-- copy, which the item holds in `blob_refs` under ('order_item', id). Null for every
-- other item, and for a `library_file` item the back office adds.
ALTER TABLE order_items
  ADD COLUMN library_sha256 text CHECK (library_sha256 ~ '^[0-9a-f]{64}$'),
  ADD COLUMN library_copy_key text;
