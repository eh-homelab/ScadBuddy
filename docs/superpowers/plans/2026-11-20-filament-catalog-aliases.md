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
- R-6706-12: an archived row keeps its names and aliases. Adding one of them again is a
  409; restore the archived row instead.
- R-6706-13: a lookup by name that meets an archived row's key returns that row. The pull,
  the CSV import and makeType link to it, and add no new row.
- R-6706-14: stored keys come only from SQL (`key_sql`, the triggers). Python
  `catalog_key` is used only against the ASCII price-book values. Under a ctype of plain
  `C`, SQL drops non-ASCII letters and Python keeps them.
- R-6706-15: the migration's merge covers all five catalogs. Ledger event JSON that names
  a deleted row's id is not rewritten.
- R-6706-16: a subtype moved to a material where its name or one of its aliases meets a
  taken key is a 409 that names the holder, as every other clash does (`detail`,
  `subtype_id`, `subtype_name`).
- R-6706-17: `+` is a word of the key: the key is
  `upper(regexp_replace(replace(name, '+', 'PLUS'), '[^[:alnum:]]', '', 'g'))`. `PLA+` and
  `PLA Plus` share the key `PLAPLUS` (one product); `PLA+` stays apart from `PLA`, and
  `Silk+` from `Silk`. It replaces R-6706-1's expression in `catalog_key`, `key_sql`, the
  migration and its trigger.
- R-6706-18: one Bambuddy or CSV catalog name with no letter or digit never stops a pull
  or an import. That field is skipped (the spool gets no link for it, as when the field is
  empty) and the rest goes on. The pull counts them in `PullReport.names_skipped`; the CSV
  preview marks the cell `{"skipped": "no letter or digit"}` beside `{"new": true}`. The
  catalog routes still refuse such a name with 422. (`+` alone is not such a name: it
  keys to `PLUS`, R-6706-17.)
- R-6706-19: `SpoolTypeStore.match` (the pull's and the type proposals' identity match)
  resolves names by key. The material by `find_by_key`; the subtype scoped to the found
  material, else with no material; the brand by `find_by_key`. The type then matches by
  `material_id`, `subtype_id` and `brand_id`. A name that resolves to nothing matches no
  type; a name with no key counts as none (R-6706-18). So an alias, or a merged loser's
  spelling (`PETG-HF` after a merge into `PETG HF`), links the existing type. No other
  name lookup on the five catalogs bypasses the key (insert types stay out, R-6706-11).
- R-6706-20: the name trigger checks for an empty key only after its "name and scope
  unchanged" return. A row the fill left without a key takes a PATCH that keeps its name;
  a keyless subtype keeps its name through a move, and only its aliases move.
- R-6706-21: `set_aliases` checks `has_key` before each INSERT, so an alias with no key is
  `AliasKeyEmptyError`, never a clash.
- R-6706-22: the type proposals match by id, not by a round trip through names.
  `SpoolTypeStore.match_ids` takes the catalog ids a group already holds (or a code alone);
  `match` by name stays for the pull. A group whose subtype is one for every material,
  while the material has its own subtype of the same key, and a group with a keyless
  catalog row, both find their existing type.
- R-6706-23: the tray reconcile compares a tray's `tray_type` by catalog key
  (`inventory.material_key` is `catalog_key`) with the keys of the spool's effective
  material, its name's and its aliases' (`material_names`). A tray that reports an alias
  or a merged loser's spelling agrees; `PLA+` no longer agrees with `PLA` (R-6706-17). A
  material with no key is unknown, never a disagreement. The CIEDE2000 colour rule is
  unchanged. (A material `PETG HF` cannot hold the alias `PETG-HF`: the two share the key
  `PETGHF`, so the tray agrees by the name's own key.)
- A name or alias with no letter or digit has no key: the trigger refuses such a name
  (422, `CatalogKeyEmptyError`), the migration neither merges nor keys an existing one,
  and a lookup never finds a row for it.

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
