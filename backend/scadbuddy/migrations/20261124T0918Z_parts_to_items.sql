-- #7067 Task 6 (spec 2026-11-21-inventory-and-purchasing-design.md §6.4-6.6, §9 phase 2):
-- the parts of #5543 (`20261107T0015Z_parts_inventory.sql`) become inventory items of the
-- kind `hardware` (the plan's Ruling 2). Each part keeps its id as its item's id, gets an
-- opening lot that holds its stock at its unit cost, and its stock moves become
-- `stock_moves` on that lot. Its supplier name becomes a supplier and an offer with the
-- part's pack price (Rulings 4 and 5). Order parts, model part rules and the part
-- entities name the items. Job cost reads part cost from the moves (Pre-flight 5), so the
-- `part:` cost entries that the moves replace are deleted. `parts` and `part_stock_moves`
-- are renamed `*_retired`; a follow-up drops them after a prod check (Ruling 6).

-- The shop's currency (`orders_currency`, default USD: `core/settings.py`). A part in
-- another currency keeps its cost, but that cost is unknown in the shop's (D11,
-- R-7067-11).
CREATE TEMP TABLE parts_migration_shop ON COMMIT DROP AS
SELECT coalesce(
    (SELECT value #>> '{}' FROM settings WHERE name = 'orders_currency'), 'USD'
) AS currency;

-- The system actor of every row this migration writes.
CREATE TEMP TABLE parts_migration_actor ON COMMIT DROP AS
SELECT '{"kind": "provider", "principal": "parts_migration"}'::jsonb AS actor;

-- 1. The item id of every part: the part's own id when it is a uuid's text (so `part_id`
-- values in activity details, order events and `entities.ref` stay valid), else a new one.
CREATE TABLE part_item_map (
    part_id text PRIMARY KEY,
    item_id uuid NOT NULL UNIQUE
);
INSERT INTO part_item_map (part_id, item_id)
SELECT id,
    CASE WHEN id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN id::uuid ELSE gen_random_uuid() END
FROM parts;

-- 2. The items.
INSERT INTO items (id, kind_id, name, low_stock, source, created_at, updated_at)
SELECT m.item_id, 'hardware', p.name, p.low_stock, 'manual', p.created_at, p.updated_at
FROM parts p JOIN part_item_map m ON m.part_id = p.id;
ALTER TABLE part_item_map ADD FOREIGN KEY (item_id) REFERENCES items (id);

-- 3. Suppliers (Ruling 5): each distinct supplier name, in any case, is the unarchived
-- supplier with that name, else a new one named as the oldest part spells it.
CREATE TEMP TABLE parts_migration_suppliers ON COMMIT DROP AS
SELECT upper(btrim(supplier)) AS key,
    (array_agg(btrim(supplier) ORDER BY created_at, id))[1] AS name,
    NULL::uuid AS supplier_id
FROM parts WHERE btrim(supplier) <> '' GROUP BY 1;
UPDATE parts_migration_suppliers t SET supplier_id = s.id
FROM filament_suppliers s WHERE s.archived_at IS NULL AND upper(s.name) = t.key;
UPDATE parts_migration_suppliers SET supplier_id = gen_random_uuid() WHERE supplier_id IS NULL;
INSERT INTO filament_suppliers (id, name)
SELECT t.supplier_id, t.name FROM parts_migration_suppliers t
WHERE NOT EXISTS (SELECT 1 FROM filament_suppliers s WHERE s.id = t.supplier_id);

-- One offer per part with a supplier. Parts have no SKU, so the offer gets one a person
-- edits; an empty link is no URL (`supplier_offers.url` must be http(s)).
INSERT INTO supplier_offers (supplier_id, item_id, sku, url, pack_size, created_at, updated_at)
SELECT t.supplier_id, m.item_id, 'PART-' || left(m.item_id::text, 8), nullif(p.link, ''),
    p.pack_size, p.created_at, p.updated_at
FROM parts p
JOIN part_item_map m ON m.part_id = p.id
JOIN parts_migration_suppliers t ON t.key = upper(btrim(p.supplier));

-- Its pack price, in the part's own currency (Ruling 4, R-7067-11). The minor unit's
-- exponent is `orders/currency.py` `minor_exponent`: these codes are its exceptions, and
-- every other code has 2.
INSERT INTO offer_price_points (offer_id, price, currency, on_date, source, created_at)
SELECT o.id,
    p.pack_price_minor::numeric / (10::numeric ^ CASE
        WHEN upper(p.currency) IN (
            'BIF', 'CLP', 'DJF', 'GNF', 'ISK', 'JPY', 'KMF', 'KRW', 'PYG', 'RWF', 'UGX',
            'UYI', 'VND', 'VUV', 'XAF', 'XOF', 'XPF',
            'XAG', 'XAU', 'XBA', 'XBB', 'XBC', 'XBD', 'XDR', 'XPD', 'XPT', 'XSU', 'XTS',
            'XUA', 'XXX'
        ) THEN 0
        WHEN upper(p.currency) IN ('BHD', 'IQD', 'JOD', 'KWD', 'LYD', 'OMR', 'TND') THEN 3
        WHEN upper(p.currency) IN ('CLF', 'UYW') THEN 4
        ELSE 2
    END),
    p.currency, (p.updated_at AT TIME ZONE 'UTC')::date, 'manual', p.updated_at
FROM supplier_offers o
JOIN part_item_map m ON m.item_id = o.item_id
JOIN parts p ON p.id = m.part_id;

-- 10 (before 4, so that step 4 finds the item's entity that a part's entity became). The
-- entities of the parts, and their files' subjects, name the items. Links and properties
-- hang off the entity row, so they follow it.
UPDATE entities e SET kind = 'item', ref = m.item_id::text
FROM part_item_map m WHERE e.kind = 'part' AND e.ref = m.part_id;
UPDATE entity_attachments a SET subject = 'item:' || m.item_id::text
FROM part_item_map m WHERE a.subject = 'part:' || m.part_id;

-- 4. A part with a link and no supplier keeps the link as a `purchase` link on its item.
INSERT INTO entities (kind, ref)
SELECT 'item', m.item_id::text
FROM parts p JOIN part_item_map m ON m.part_id = p.id
WHERE btrim(p.supplier) = '' AND p.link <> ''
ON CONFLICT (kind, ref) DO NOTHING;
INSERT INTO entity_links (entity_id, url, kind, position, source, created_by)
SELECT e.id, p.link, 'purchase',
    (SELECT coalesce(max(l.position) + 1, 0) FROM entity_links l WHERE l.entity_id = e.id),
    'parts_migration', a.actor
FROM parts p
JOIN part_item_map m ON m.part_id = p.id
JOIN entities e ON e.kind = 'item' AND e.ref = m.item_id::text
CROSS JOIN parts_migration_actor a
WHERE btrim(p.supplier) = '' AND p.link <> '';

-- 5. One opening lot per part. Its intake is the part's on hand: a part's first count is
-- an `adjust`, not an `add` (`PartStore._create`), and the history before the lot is kept
-- as its moves (step 6). A part in another currency than the shop's opens with its cost
-- unknown; `foreign_unit_cost_minor` keeps that cost as a foreign receive does (R-P2-11).
INSERT INTO stock_lots (item_id, received_qty, on_hand, unit_cost_minor, cost_unknown,
    cost_currency, foreign_unit_cost_minor, origin, note, received_at, created_by)
SELECT m.item_id, p.on_hand, p.on_hand, p.unit_cost_minor, p.currency <> s.currency,
    p.currency, CASE WHEN p.currency <> s.currency THEN p.unit_cost_minor END,
    'opening', 'The stock of the part before parts became items (#7067)', p.created_at, a.actor
FROM parts p
JOIN part_item_map m ON m.part_id = p.id
CROSS JOIN parts_migration_shop s
CROSS JOIN parts_migration_actor a;

-- 6. Every move that moved stock, on its part's lot. A move of 0 (a count equal to the
-- shelf, `PartStore.adjust`) moved nothing, and `stock_moves` refuses qty 0; the retired
-- table keeps its note. A `use` or an `adjust` costs the part's unit cost then (what
-- `PartStore.used_at` reads); an `add` costs what its packs cost per unit, as
-- `unit_cost_after_minor` is the weighted average after it.
CREATE TEMP TABLE parts_migration_moves ON COMMIT DROP AS
SELECT gen_random_uuid() AS id, sm.id AS old_id, sm.part_id, m.item_id,
    CASE sm.kind WHEN 'add' THEN 'receive' ELSE sm.kind END AS kind,
    sm.delta::numeric AS qty,
    CASE WHEN sm.kind = 'add' AND sm.amount_minor IS NOT NULL
        THEN round(sm.amount_minor::numeric / sm.delta, 6)
        ELSE sm.unit_cost_after_minor END AS unit_cost_minor,
    sm.amount_minor AS old_amount_minor, sm.note, sm.order_id, sm.order_item_id,
    sm.created_by, sm.created_at, NULL::uuid AS reverses_move_id
FROM part_stock_moves sm JOIN part_item_map m ON m.part_id = sm.part_id
WHERE sm.delta <> 0;

-- A `return` reverses the newest `use` of its order item and part before it that no
-- return reversed yet, and takes that use's unit cost, so the pair nets to 0. A return
-- with no such use keeps the part's unit cost after it.
DO $$
DECLARE
    r record;
    use_id uuid;
    use_cost numeric;
BEGIN
    FOR r IN
        SELECT id, part_id, order_item_id, created_at, old_id FROM parts_migration_moves
        WHERE kind = 'return' ORDER BY created_at, old_id
    LOOP
        SELECT u.id, u.unit_cost_minor INTO use_id, use_cost
        FROM parts_migration_moves u
        WHERE u.kind = 'use' AND u.part_id = r.part_id AND u.order_item_id = r.order_item_id
            AND (u.created_at, u.old_id) < (r.created_at, r.old_id)
            AND NOT EXISTS (
                SELECT 1 FROM parts_migration_moves x WHERE x.reverses_move_id = u.id
            )
        ORDER BY u.created_at DESC, u.old_id DESC
        LIMIT 1;
        IF FOUND THEN
            UPDATE parts_migration_moves SET reverses_move_id = use_id, unit_cost_minor = use_cost
            WHERE id = r.id;
        END IF;
    END LOOP;
END
$$;

-- A move's cost is unknown when its part's currency is not the shop's (its minor units
-- are not the shop's), or when it is a use or return that `PartStore.move` wrote with no
-- amount at a cost above 0 (it took the units with no cost the order could carry).
INSERT INTO stock_moves (id, at, item_id, lot_id, kind, qty, unit_cost_minor, amount_minor,
    cost_unknown, reverses_move_id, reason, order_id, order_item_id, actor)
SELECT mv.id, mv.created_at, mv.item_id, l.id, mv.kind, mv.qty, mv.unit_cost_minor,
    round(mv.qty * mv.unit_cost_minor, 6),
    p.currency <> s.currency
        OR (mv.kind IN ('use', 'return') AND mv.old_amount_minor IS NULL
            AND mv.unit_cost_minor > 0),
    mv.reverses_move_id, mv.note, mv.order_id, mv.order_item_id, mv.created_by
FROM parts_migration_moves mv
JOIN parts p ON p.id = mv.part_id
JOIN stock_lots l ON l.item_id = mv.item_id AND l.origin = 'opening'
CROSS JOIN parts_migration_shop s;

-- 7. The `part:<order item>:<part>` cost entries that a converted `use` replaces: job
-- cost reads the use's move now, so no order counts a part twice (Pre-flight 5).
DELETE FROM order_cost_entries e
WHERE e.kind = 'material' AND e.source LIKE 'part:%'
    AND EXISTS (
        SELECT 1 FROM part_stock_moves sm
        WHERE sm.kind = 'use' AND sm.delta <> 0
            AND e.source = 'part:' || sm.order_item_id || ':' || sm.part_id
    );

-- 8. An order item's parts name items.
ALTER TABLE order_item_parts ADD COLUMN item_id uuid REFERENCES items (id);
UPDATE order_item_parts ip SET item_id = m.item_id
FROM part_item_map m WHERE m.part_id = ip.part_id;
ALTER TABLE order_item_parts ALTER COLUMN item_id SET NOT NULL;
ALTER TABLE order_item_parts DROP CONSTRAINT order_item_parts_pkey;
DROP INDEX order_item_parts_part;
ALTER TABLE order_item_parts DROP COLUMN part_id;
ALTER TABLE order_item_parts ADD PRIMARY KEY (order_item_id, item_id);
CREATE INDEX order_item_parts_item ON order_item_parts (item_id);

-- 9. A model's rule names items: each line's `part_id` and each `when`'s become
-- `item_id`. `PartStore.set_rule` saves only parts that exist, so every id has an item;
-- one that does not stops the migration rather than lose a rule line.
DO $$
DECLARE
    missing text;
BEGIN
    SELECT ids.part_id INTO missing
    FROM model_part_rules r,
        LATERAL (
            SELECT line ->> 'part_id' AS part_id FROM jsonb_array_elements(r.lines) line
            UNION ALL
            SELECT w ->> 'part_id' FROM jsonb_array_elements(r.lines) line,
                jsonb_array_elements(coalesce(line -> 'when', '[]')) w
        ) ids
    WHERE ids.part_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM part_item_map m WHERE m.part_id = ids.part_id)
    LIMIT 1;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'model_part_rules names part %, which does not exist', missing;
    END IF;
END
$$;
UPDATE model_part_rules r SET lines = (
    SELECT coalesce(jsonb_agg(
        (line - 'part_id' - 'when')
        || jsonb_build_object(
            'item_id',
            (SELECT to_jsonb(m.item_id::text) FROM part_item_map m
                WHERE m.part_id = line ->> 'part_id'),
            'when',
            (SELECT coalesce(jsonb_agg(
                (w - 'part_id') || jsonb_build_object(
                    'item_id',
                    (SELECT to_jsonb(m.item_id::text) FROM part_item_map m
                        WHERE m.part_id = w ->> 'part_id')
                ) ORDER BY wn), '[]'::jsonb)
             FROM jsonb_array_elements(coalesce(line -> 'when', '[]')) WITH ORDINALITY ws (w, wn))
        ) ORDER BY n), '[]'::jsonb)
    FROM jsonb_array_elements(r.lines) WITH ORDINALITY ls (line, n)
);

-- 11. The old tables, kept until a follow-up drops them after a prod check (Ruling 6).
ALTER TABLE parts RENAME TO parts_retired;
ALTER TABLE part_stock_moves RENAME TO part_stock_moves_retired;
