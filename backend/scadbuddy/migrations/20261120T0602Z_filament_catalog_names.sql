-- Catalog names and aliases (#6706, plan docs/superpowers/plans/2026-11-20-filament-catalog-aliases.md).
-- The materials, subtypes, brands, categories and effects catalogs each get one table
-- that holds every row's name and every alias, keyed by
-- `upper(regexp_replace(replace(name, '+', 'PLUS'), '[^[:alnum:]]', '', 'g'))`
-- (`filament/catalog.py` `key_sql`), so `Dual Color`, `dual-color` and `DUAL_COLOR` are one
-- key, and `PLA+` is `PLA Plus` but not `PLA` (R-6706-17). A name with no letter or digit
-- has no key: the trigger refuses it, and the merge and the fill leave such a row alone. The
-- primary key keeps a key on one row; a subtype's key is unique within its material
-- (`material_scope`: its `material_id`, or the nil uuid for a subtype of every material).
-- A key stays reserved while its row is archived.
--
-- The old `upper(name) WHERE archived_at IS NULL` indexes go: the keys are stricter, and
-- the subtypes' index would refuse the material merge below.
--
-- Rows whose keys collide merge into the survivor: the lowest `sort_order`, then the
-- earliest `created_at`, then the lowest id. Every foreign key to the catalog moves to
-- it; they are read from `pg_constraint`, not listed by hand. On 2026-11-20 they were:
--   filament_materials:  filament_subtypes, filament_colours, filament_spool_types,
--                        filament_spools (material_id)
--   filament_subtypes:   filament_spool_types, filament_spools (subtype_id)
--   filament_brands:     filament_spool_types, filament_spools (brand_id)
--   filament_categories: filament_spool_types, filament_spools (category_id)
--   filament_effects:    filament_colours, filament_spool_types, filament_spools (effect_id)
-- Materials merge before subtypes, since a material merge moves subtypes into the
-- survivor's scope, where they may collide. A survivor is restored when a loser was live.
-- The loser's name becomes an alias of the survivor, unless its key is the survivor's
-- own (it then resolves already), and the loser is deleted.
--
-- Then a trigger on each catalog claims the canonical key on insert and re-keys it on a
-- change of `name` (or a subtype's `material_id`), in the writer's transaction. A clash
-- is a unique violation on the names table. Every statement here can run again.

DROP INDEX IF EXISTS filament_materials_name;
DROP INDEX IF EXISTS filament_subtypes_name;
DROP INDEX IF EXISTS filament_brands_name;
DROP INDEX IF EXISTS filament_categories_name;
DROP INDEX IF EXISTS filament_effects_name;

CREATE TABLE IF NOT EXISTS filament_material_names (
    key text PRIMARY KEY,
    material_id uuid NOT NULL REFERENCES filament_materials (id) ON DELETE CASCADE,
    name text NOT NULL,
    canonical boolean NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS filament_material_names_canonical
    ON filament_material_names (material_id) WHERE canonical;
CREATE INDEX IF NOT EXISTS filament_material_names_owner ON filament_material_names (material_id);

CREATE TABLE IF NOT EXISTS filament_subtype_names (
    material_scope uuid NOT NULL,
    key text NOT NULL,
    subtype_id uuid NOT NULL REFERENCES filament_subtypes (id) ON DELETE CASCADE,
    name text NOT NULL,
    canonical boolean NOT NULL,
    PRIMARY KEY (material_scope, key)
);
CREATE UNIQUE INDEX IF NOT EXISTS filament_subtype_names_canonical
    ON filament_subtype_names (subtype_id) WHERE canonical;
CREATE INDEX IF NOT EXISTS filament_subtype_names_owner ON filament_subtype_names (subtype_id);

CREATE TABLE IF NOT EXISTS filament_brand_names (
    key text PRIMARY KEY,
    brand_id uuid NOT NULL REFERENCES filament_brands (id) ON DELETE CASCADE,
    name text NOT NULL,
    canonical boolean NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS filament_brand_names_canonical
    ON filament_brand_names (brand_id) WHERE canonical;
CREATE INDEX IF NOT EXISTS filament_brand_names_owner ON filament_brand_names (brand_id);

CREATE TABLE IF NOT EXISTS filament_category_names (
    key text PRIMARY KEY,
    category_id uuid NOT NULL REFERENCES filament_categories (id) ON DELETE CASCADE,
    name text NOT NULL,
    canonical boolean NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS filament_category_names_canonical
    ON filament_category_names (category_id) WHERE canonical;
CREATE INDEX IF NOT EXISTS filament_category_names_owner ON filament_category_names (category_id);

CREATE TABLE IF NOT EXISTS filament_effect_names (
    key text PRIMARY KEY,
    effect_id uuid NOT NULL REFERENCES filament_effects (id) ON DELETE CASCADE,
    name text NOT NULL,
    canonical boolean NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS filament_effect_names_canonical
    ON filament_effect_names (effect_id) WHERE canonical;
CREATE INDEX IF NOT EXISTS filament_effect_names_owner ON filament_effect_names (effect_id);

DO $migration$
DECLARE
    c record;
    fk record;
    scope text;
    scope_column text;
BEGIN
    FOR c IN
        SELECT * FROM (VALUES
            (1, 'filament_materials', 'filament_material_names', 'material_id', false),
            (2, 'filament_brands', 'filament_brand_names', 'brand_id', false),
            (3, 'filament_categories', 'filament_category_names', 'category_id', false),
            (4, 'filament_effects', 'filament_effect_names', 'effect_id', false),
            (5, 'filament_subtypes', 'filament_subtype_names', 'subtype_id', true)
        ) AS t (ord, catalog, names, owner, scoped)
        ORDER BY ord
    LOOP
        scope := CASE WHEN c.scoped
            THEN 'coalesce(material_id, ''00000000-0000-0000-0000-000000000000''::uuid)'
            ELSE 'NULL::uuid' END;
        scope_column := CASE WHEN c.scoped THEN 'material_scope, ' ELSE '' END;

        DROP TABLE IF EXISTS pg_temp.catalog_merge;
        EXECUTE format(
            'CREATE TEMP TABLE catalog_merge AS'
            ' SELECT id AS loser, survivor, name, archived_at IS NULL AS live FROM ('
            '   SELECT id, name, archived_at, first_value(id) OVER ('
            '     PARTITION BY %s, upper(regexp_replace(replace(name, ''+'', ''PLUS''), ''[^[:alnum:]]'', '''', ''g'')),'
            '       CASE WHEN upper(regexp_replace(replace(name, ''+'', ''PLUS''), ''[^[:alnum:]]'', '''', ''g'')) = '''' THEN id END'
            '     ORDER BY sort_order, created_at, id) AS survivor'
            '   FROM %I) ranked'
            ' WHERE id <> survivor',
            scope, c.catalog);

        FOR fk IN
            SELECT con.conrelid::regclass AS tbl, con.conkey, a.attname AS col
            FROM pg_constraint con
            JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
            WHERE con.contype = 'f'
              AND con.confrelid = c.catalog::regclass
              AND con.conrelid <> c.names::regclass
        LOOP
            IF array_length(fk.conkey, 1) <> 1 THEN
                RAISE EXCEPTION 'cannot merge %: % has a foreign key of more than one column',
                    c.catalog, fk.tbl;
            END IF;
            EXECUTE format(
                'UPDATE %s t SET %I = m.survivor FROM catalog_merge m WHERE t.%I = m.loser',
                fk.tbl, fk.col, fk.col);
        END LOOP;

        EXECUTE format(
            'UPDATE %I t SET archived_at = NULL FROM ('
            '  SELECT survivor FROM catalog_merge GROUP BY survivor HAVING bool_or(live)'
            ') s WHERE t.id = s.survivor AND t.archived_at IS NOT NULL',
            c.catalog);

        EXECUTE format('DELETE FROM %I WHERE id IN (SELECT loser FROM catalog_merge)', c.catalog);

        EXECUTE format(
            'INSERT INTO %I (%skey, %I, name, canonical)'
            ' SELECT %s upper(regexp_replace(replace(name, ''+'', ''PLUS''), ''[^[:alnum:]]'', '''', ''g'')), id, name, true'
            ' FROM %I WHERE upper(regexp_replace(replace(name, ''+'', ''PLUS''), ''[^[:alnum:]]'', '''', ''g'')) <> '''' ON CONFLICT DO NOTHING',
            c.names, scope_column, c.owner,
            CASE WHEN c.scoped THEN scope || ',' ELSE '' END, c.catalog);

        EXECUTE format(
            'INSERT INTO %I (%skey, %I, name, canonical)'
            ' SELECT %s upper(regexp_replace(replace(m.name, ''+'', ''PLUS''), ''[^[:alnum:]]'', '''', ''g'')), m.survivor,'
            '   m.name, false'
            ' FROM catalog_merge m JOIN %I s ON s.id = m.survivor ON CONFLICT DO NOTHING',
            c.names, scope_column, c.owner,
            CASE WHEN c.scoped
                THEN 'coalesce(s.material_id, ''00000000-0000-0000-0000-000000000000''::uuid),'
                ELSE '' END,
            c.catalog);

        DROP TABLE pg_temp.catalog_merge;
    END LOOP;
END
$migration$;

-- TG_ARGV: the names table, its owner column, and whether keys are scoped by material.
CREATE OR REPLACE FUNCTION filament_catalog_claim_name() RETURNS trigger
LANGUAGE plpgsql AS $claim$
DECLARE
    names text := TG_ARGV[0];
    owner text := TG_ARGV[1];
    scoped boolean := TG_ARGV[2]::boolean;
    nil constant uuid := '00000000-0000-0000-0000-000000000000';
    new_key text := upper(regexp_replace(replace(NEW.name, '+', 'PLUS'), '[^[:alnum:]]', '', 'g'));
    new_scope uuid := nil;
    old_scope uuid := nil;
BEGIN
    IF scoped THEN
        new_scope := coalesce((to_jsonb(NEW) ->> 'material_id')::uuid, nil);
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF scoped THEN
            old_scope := coalesce((to_jsonb(OLD) ->> 'material_id')::uuid, nil);
        END IF;
        IF NEW.name IS NOT DISTINCT FROM OLD.name AND new_scope = old_scope THEN
            RETURN NULL;
        END IF;
        -- A row the fill left without a key (R-6706-14) keeps its name through a move: it
        -- has no canonical key to write, only its aliases move.
        IF new_key = '' AND NEW.name IS NOT DISTINCT FROM OLD.name THEN
            EXECUTE format('UPDATE %I SET material_scope = $2 WHERE %I = $1', names, owner)
                USING NEW.id, new_scope;
            RETURN NULL;
        END IF;
    END IF;
    IF new_key = '' THEN
        RAISE EXCEPTION 'the name % has no letter or digit', NEW.name
            USING ERRCODE = 'check_violation', TABLE = TG_TABLE_NAME;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        EXECUTE format('DELETE FROM %I WHERE %I = $1 AND canonical', names, owner)
            USING NEW.id;
        IF new_scope <> old_scope THEN
            EXECUTE format('UPDATE %I SET material_scope = $2 WHERE %I = $1', names, owner)
                USING NEW.id, new_scope;
        END IF;
        -- A name that takes one of the row's own aliases' keys replaces that alias.
        IF scoped THEN
            EXECUTE format(
                'DELETE FROM %I WHERE %I = $1 AND material_scope = $2 AND key = $3', names, owner)
                USING NEW.id, new_scope, new_key;
        ELSE
            EXECUTE format('DELETE FROM %I WHERE %I = $1 AND key = $2', names, owner)
                USING NEW.id, new_key;
        END IF;
    END IF;
    IF scoped THEN
        EXECUTE format(
            'INSERT INTO %I (material_scope, key, %I, name, canonical) VALUES ($1, $2, $3, $4, true)',
            names, owner)
            USING new_scope, new_key, NEW.id, NEW.name;
    ELSE
        EXECUTE format(
            'INSERT INTO %I (key, %I, name, canonical) VALUES ($1, $2, $3, true)', names, owner)
            USING new_key, NEW.id, NEW.name;
    END IF;
    RETURN NULL;
END
$claim$;

CREATE OR REPLACE TRIGGER filament_materials_claim_name
    AFTER INSERT OR UPDATE OF name ON filament_materials
    FOR EACH ROW EXECUTE FUNCTION filament_catalog_claim_name('filament_material_names', 'material_id', 'false');
CREATE OR REPLACE TRIGGER filament_subtypes_claim_name
    AFTER INSERT OR UPDATE OF name, material_id ON filament_subtypes
    FOR EACH ROW EXECUTE FUNCTION filament_catalog_claim_name('filament_subtype_names', 'subtype_id', 'true');
CREATE OR REPLACE TRIGGER filament_brands_claim_name
    AFTER INSERT OR UPDATE OF name ON filament_brands
    FOR EACH ROW EXECUTE FUNCTION filament_catalog_claim_name('filament_brand_names', 'brand_id', 'false');
CREATE OR REPLACE TRIGGER filament_categories_claim_name
    AFTER INSERT OR UPDATE OF name ON filament_categories
    FOR EACH ROW EXECUTE FUNCTION filament_catalog_claim_name('filament_category_names', 'category_id', 'false');
CREATE OR REPLACE TRIGGER filament_effects_claim_name
    AFTER INSERT OR UPDATE OF name ON filament_effects
    FOR EACH ROW EXECUTE FUNCTION filament_catalog_claim_name('filament_effect_names', 'effect_id', 'false');
