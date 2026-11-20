# Filament catalog aliases (#6706)

Goal: a row of the materials, subtypes, brands, categories and effects catalogs has one
canonical name and any number of aliases. Each catalog has one key space for its names
and aliases, and the database keeps each key unique. A known spelling never makes a new
row.

## Decisions

- R-6706-1: the key is `upper(regexp_replace(name, '[^[:alnum:]]', '', 'g'))`. Python
  (`filament/catalog.py` `catalog_key`) and SQL (`key_sql`) use the same function, and a
  test checks that they agree.
- R-6706-2: one name table per catalog, of the shape `filament_effect_names(key text
  PRIMARY KEY, effect_id uuid NOT NULL REFERENCES filament_effects ON DELETE CASCADE, name
  text NOT NULL, canonical boolean NOT NULL)`, with a partial unique index on `(effect_id)
  WHERE canonical`. Every write to a row's name or aliases goes through it in the same
  transaction, so the primary key enforces uniqueness.
- R-6706-3: the migration fills the tables from the rows that exist, then merges colliding
  keys. The survivor is the lowest `sort_order`, then the earliest `created_at`, then the
  lowest id. Every foreign key to the catalog moves to the survivor; the migration finds
  them in `pg_constraint`, so none is missed, and lists them in a comment. The loser's
  name becomes an alias, and the loser is deleted. It is idempotent and safe on an empty
  database.
- R-6706-4: the name lookups (pull, CSV, price book, `makeTypeFromBook` through the API)
  resolve by key. A missing key still adds a new row, as today, with its key row.
- R-6706-5: archiving a row keeps its keys reserved.
- R-6706-6: the API returns `aliases: list[str]` on each row. A PUT sets the alias list,
  and PATCH `name` re-keys the canonical row. A clash is 409 with `{detail, <kind>_id,
  <kind>_name}` (`effect_id`, `effect_name` for an effect). Aliases are trimmed. An empty
  alias is 422.
- R-6706-7: the UI shows aliases as chips in each of these catalogs' editors (add, remove,
  inline 409 text). Pickers show canonical names only. It works at 390 px.
- R-6706-8 (replaces "effects only"): aliases and the key space apply to materials,
  subtypes, brands, categories and effects.
- R-6706-9: one name table per catalog, of the same shape: `filament_material_names`,
  `filament_subtype_names`, `filament_brand_names`, `filament_category_names` and
  `filament_effect_names`. One shared helper, one migration file.
- R-6706-10: a subtype's key is unique within its material. The primary key is
  `(material_scope, key)`; `material_scope` is the subtype's `material_id`, or the nil
  uuid for a subtype with no material. The merge merges only within one scope.
- R-6706-11: insert types and insert materials are out of scope. Insert types match by
  `bb_catalog_id` first.

Consequences found while building:

- Triggers on the five catalog tables write the canonical key row (on insert, and on a
  change of `name` or a subtype's `material_id`), so every writer, SQL included, goes
  through the key table in its own transaction. Aliases are written by the store.
- The old `upper(name) WHERE archived_at IS NULL` unique indexes of the five tables are
  dropped: the key tables are stricter (a key is reserved while archived, R-6706-5), and
  the subtypes' index would refuse the material merge's moves.
- Materials merge before subtypes: a material merge moves subtypes into the survivor's
  scope, where they can then collide and merge.
- A merged loser whose key equals the survivor's (every collision the migration can see)
  adds no alias row: its key is already the survivor's, so the spelling resolves.
- A name lookup that finds an archived row's key returns that row (R-6706-5 keeps the key,
  so a new row with that key cannot exist).
- A survivor that is archived while a merged loser was not is restored, so a live row does
  not vanish from the pickers.
- An alias whose key is empty (only punctuation) is 422, as an empty alias is.
- Renaming a row to one of its own aliases' keys drops that alias: the name takes the key.

## Steps

1. Backend: key helper, migration (tables, merge, triggers), store (aliases), lookups
   (pull, CSV, price book, resolve route), routes and the 409 body. Tests first.
2. Frontend: API helpers, MSW mocks, alias chips in the five editors, `makeTypeFromBook`
   through the resolve route. Vitest first.
3. Agent: the catalog tools carry aliases; a tool sets them. Tests first.
