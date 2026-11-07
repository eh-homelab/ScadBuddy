-- #959: STL, 3MF and OBJ uploads for `// file` parameters (library/meshes.py), with
-- each mesh's triangle count and bounding box (min x, y, z, then max x, y, z).
ALTER TABLE assets DROP CONSTRAINT assets_kind_check;
ALTER TABLE assets ADD CONSTRAINT assets_kind_check
    CHECK (kind IN ('svg', 'png', 'stl', '3mf', 'obj'));
ALTER TABLE assets
    ADD COLUMN triangles integer CHECK (triangles > 0),
    ADD COLUMN bounds double precision[] CHECK (cardinality(bounds) = 6);
