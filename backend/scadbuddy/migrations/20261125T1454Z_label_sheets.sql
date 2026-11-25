-- #7216 R-7216-11: saved label sheet grids. Only the API reads and writes this table.
-- `name_key` is the name after NFC and casefold (`filament/label_sheets.py` `name_key`):
-- its unique index keeps two saves of one name at once from both passing. The API
-- checks the other bounds before it writes a row.
CREATE TABLE label_sheets (
    id          uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text             NOT NULL,
    name_key    text             NOT NULL UNIQUE,
    page        text             NOT NULL CHECK (page IN ('a4', 'letter')),
    width_mm    double precision NOT NULL,
    height_mm   double precision NOT NULL,
    cols        integer          NOT NULL CHECK (cols >= 1),
    rows        integer          NOT NULL CHECK (rows >= 1),
    top_mm      double precision NOT NULL,
    left_mm     double precision NOT NULL,
    col_gap_mm  double precision NOT NULL,
    row_gap_mm  double precision NOT NULL,
    created_at  timestamptz      NOT NULL DEFAULT now(),
    updated_at  timestamptz      NOT NULL DEFAULT now()
);
