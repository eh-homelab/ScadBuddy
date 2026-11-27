-- #8155: each scan of a code from a photo. core writes these rows (tools/go/core/internal/scan).
-- The photo is a `scan_image` blob, held in `blob_refs` as (`code_scan`, <id>) for as long as
-- the row lives, so a failed decode can be decoded again. `codes` is what the decode found,
-- `[{"format": …, "text": …}]`; `error` says why it found nothing it could read
-- (`image_unreadable`, `image_too_large`), else NULL. The place is the page's GPS fix, when it
-- had one.
CREATE TABLE code_scans (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    at          timestamptz NOT NULL DEFAULT now(),
    actor       text,
    source      text NOT NULL CHECK (source IN ('shutter', 'nfc', 'file', 'app')),
    image_key   text NOT NULL,
    media_type  text NOT NULL,
    width       integer CHECK (width > 0),
    height      integer CHECK (height > 0),
    codes       jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(codes) = 'array'),
    error       text CHECK (error IN ('image_unreadable', 'image_too_large')),
    decoded_at  timestamptz NOT NULL DEFAULT now(),
    lat         double precision CHECK (lat BETWEEN -90 AND 90),
    lon         double precision CHECK (lon BETWEEN -180 AND 180),
    accuracy_m  double precision CHECK (accuracy_m BETWEEN 0 AND 1000000),
    located_at  timestamptz,
    CHECK ((lat IS NULL) = (lon IS NULL)),
    CHECK (lat IS NOT NULL OR (accuracy_m IS NULL AND located_at IS NULL))
);
CREATE INDEX code_scans_at ON code_scans (at DESC, id DESC);
