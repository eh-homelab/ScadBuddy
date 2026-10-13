-- A finished piece's geometry, by a key its colour parameters do not enter
-- (`render/geometry_cache.py` `geometry_key`): a later piece that differs only in colour
-- copies that piece's meshes and is recoloured, instead of rendered again. `colours` is
-- what the piece's colour classes were rendered in, `main` its `render_main` result.
-- The row is a hint: the piece it names may have been swept, which is a miss.
CREATE TABLE IF NOT EXISTS piece_geometry (
    geometry_key text PRIMARY KEY,
    piece_key text NOT NULL,
    colours jsonb NOT NULL,
    main jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
