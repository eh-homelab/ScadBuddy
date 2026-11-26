-- Inventory K1 (#6949; design 2026-11-25-inventory-kinds-and-handlers-design.md §3, §6.3,
-- §8; the proof docs/inventory-model/kinds.yaml and relations.yaml). A kind gets a class
-- (`nature`, `role`, `counting`, `billing`), a special type and `steps`; an item and a
-- unit get `steps`. Relationships, the parameter history, the operation rows and the
-- migration's report are new tables. Then every phase 2 row is placed in the new model by
-- the §8 rules and the plan's R-K1-9 to R-K1-11, and each rule writes one report row.
--
-- A kind's class columns hold only the keys it answers itself (C22). A child may answer a
-- key its parent left open, never change one its parent answered (§3). So `printer` sets
-- none: `durable` gives it physical, durable, unit.

-- No inventory write may commit while the rows move (R-P2-20). A pod of the previous
-- build waits here.
LOCK TABLE item_kinds, items, stock_lots, stock_units IN EXCLUSIVE MODE;

-- §6.3 The class, the special type and the steps. Every class column is nullable: a kind
-- leaves a key open for its children (§3).
ALTER TABLE item_kinds
    ADD COLUMN nature text CHECK (nature IN ('physical', 'service')),
    ADD COLUMN role text CHECK (role IN ('consumable', 'durable')),
    ADD COLUMN counting text CHECK (counting IN ('lot', 'unit')),
    ADD COLUMN billing text CHECK (billing IN ('term', 'hourly', 'flat')),
    ADD COLUMN special_type text CHECK (special_type ~ '^[a-z][a-z0-9_]{0,39}$'),
    ADD COLUMN steps jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(steps) = 'object');

ALTER TABLE items ADD COLUMN steps jsonb NOT NULL DEFAULT '{}';

-- A service term runs from `starts_at` to `ends_at` (`life.term`, §4.5) and then ends.
ALTER TABLE stock_units
    ADD COLUMN steps jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN starts_at timestamptz,
    ADD COLUMN ends_at timestamptz;
ALTER TABLE stock_units DROP CONSTRAINT stock_units_status_check;
ALTER TABLE stock_units ADD CONSTRAINT stock_units_status_check
    CHECK (status IN ('in_stock', 'in_service', 'retired', 'scrapped', 'sold', 'ended'));

-- §5 The relationship types. `from_rule` and `to_rule` say what may stand on each end
-- (`target`, `class`, `special`); a type with no handler is shown only.
CREATE TABLE relation_types (
    id text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9_]{0,39}$'),
    label text NOT NULL,
    inverse_label text NOT NULL,
    from_rule jsonb NOT NULL,
    to_rule jsonb NOT NULL,
    max_from_per_to int,
    handler text,
    attrs text[] NOT NULL DEFAULT '{}',
    builtin boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO relation_types
    (id, label, inverse_label, from_rule, to_rule, max_from_per_to, handler, attrs, builtin)
VALUES
    ('accepts', 'accepts', 'fits in',
     '{"target": ["kind", "item", "unit"], "class": {"nature": "physical"}}',
     '{"target": ["kind", "item"]}', NULL, NULL, '{max,where}', true),
    ('contains', 'contains', 'installed in',
     '{"target": ["unit"], "class": {"nature": "physical"}}',
     '{"target": ["unit"], "class": {"nature": "physical"}}', 1, 'relation.contains', '{}', true),
    ('covers', 'covers', 'covered by',
     '{"target": ["unit"], "class": {"nature": "service", "billing": "term"}}',
     '{"target": ["unit", "item"]}', NULL, 'relation.covers', '{}', true),
    ('compatible_with', 'compatible with', 'takes',
     '{"target": ["item", "kind"]}', '{"target": ["item", "kind"]}', NULL, NULL, '{}', true),
    ('continues_from', 'continues from', 'continued as',
     '{"target": ["unit"]}', '{"target": ["unit"]}', 1, NULL, '{}', true);

-- §6.3 A relationship joins two entities. Its foreign keys keep an entity it names: the
-- entities sweep skips such an entity (`entities/registry.py` `orphans`).
CREATE TABLE relations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type_id text NOT NULL REFERENCES relation_types (id),
    from_entity uuid NOT NULL REFERENCES entities (id),
    to_entity uuid NOT NULL REFERENCES entities (id),
    attrs jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by jsonb NOT NULL,
    ended_at timestamptz,
    CHECK (from_entity <> to_entity)
);
CREATE UNIQUE INDEX relations_open ON relations (type_id, from_entity, to_entity)
    WHERE ended_at IS NULL;
CREATE INDEX relations_to ON relations (to_entity) WHERE ended_at IS NULL;

-- §4.7 The parameter history: one row per `handler_config.changed` event, keyed by its
-- `seq`. Only the history subscriber writes it (R-K1-13). `owner` is `item_kind:<id>`,
-- `item:<uuid>` or `unit:<uuid>`.
CREATE TABLE handler_config_history (
    event_seq bigint PRIMARY KEY,
    owner text NOT NULL,
    step text NOT NULL,
    handler text,
    old jsonb,
    new jsonb,
    at timestamptz NOT NULL,
    actor jsonb NOT NULL
);
CREATE INDEX handler_config_history_owner ON handler_config_history (owner, step, event_seq);

-- What this migration placed, one row per rule (R-K1-9). K2 drops it.
CREATE TABLE inventory_k1_report (
    rule text PRIMARY KEY,
    count int NOT NULL,
    detail jsonb NOT NULL DEFAULT '[]'
);

-- §6.1a One row per inventory activity (R-K1-22). Only Go writes it. `id` is the activity
-- id, `kind` the activity name (`inventory.Take`), `subject` what it acts on
-- (`item:<id>`, `lot:<id>`, `unit:<id>`, `item_kind:<id>`, `printer:<id>`).
-- `workflow_id` is set when a workflow ran the activity, NULL for a standalone one. The
-- result body stays in `crud_requests` (7 days); the row keeps the problem.
CREATE TABLE inventory_operations (
    id text PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 300),
    kind text NOT NULL,
    subject text NOT NULL DEFAULT '',
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    workflow_id text,
    attempt int NOT NULL DEFAULT 1,
    actor jsonb,
    problem jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);
CREATE INDEX inventory_operations_status ON inventory_operations (status, updated_at);

-- The system actor of every row this migration writes.
CREATE TEMP TABLE inventory_k1_actor ON COMMIT DROP AS
SELECT '{"kind": "provider", "principal": "inventory_k1_migration"}'::jsonb AS actor;

-- Each kind with its ancestors, the kind itself at depth 0. A view, so each read sees the
-- parents as the steps below change them.
CREATE TEMP VIEW inventory_k1_chain AS
WITH RECURSIVE chain (kind_id, anc_id, depth) AS (
    SELECT id, id, 0 FROM item_kinds
    UNION ALL
    SELECT c.kind_id, k.parent_id, c.depth + 1
    FROM chain c JOIN item_kinds k ON k.id = c.anc_id
    WHERE k.parent_id IS NOT NULL
)
SELECT kind_id, anc_id, depth FROM chain;

-- Each kind's class and special type, read root first (a child only answers what its
-- parent left open), and the `life` handler its kinds name, read leaf first (§4.2 layer
-- 3). No special type names a life handler (`handlers.yaml` `special_types`), so this is
-- the kind's life handler unless it is the class default.
CREATE TEMP VIEW inventory_k1_class AS
SELECT c.kind_id,
    (array_agg(a.nature ORDER BY c.depth DESC) FILTER (WHERE a.nature IS NOT NULL))[1] AS nature,
    (array_agg(a.role ORDER BY c.depth DESC) FILTER (WHERE a.role IS NOT NULL))[1] AS role,
    (array_agg(a.counting ORDER BY c.depth DESC)
        FILTER (WHERE a.counting IS NOT NULL))[1] AS counting,
    (array_agg(a.billing ORDER BY c.depth DESC) FILTER (WHERE a.billing IS NOT NULL))[1] AS billing,
    (array_agg(a.special_type ORDER BY c.depth DESC)
        FILTER (WHERE a.special_type IS NOT NULL))[1] AS special_type,
    (array_agg(a.steps -> 'life' ->> 'handler' ORDER BY c.depth)
        FILTER (WHERE a.steps -> 'life' ? 'handler'))[1] AS life
FROM inventory_k1_chain c JOIN item_kinds a ON a.id = c.anc_id
GROUP BY c.kind_id;

-- Each kind's properties merged root to leaf, a child's key winning, as the phase 2 rule
-- reads an item's schema (`inventory/kinds.py` `merged_schema`).
CREATE TEMP VIEW inventory_k1_props AS
SELECT c.kind_id, jsonb_object_agg(p.key, p.value ORDER BY c.depth DESC) AS props
FROM inventory_k1_chain c
JOIN item_kinds a ON a.id = c.anc_id
CROSS JOIN LATERAL jsonb_each(coalesce(a.attribute_schema -> 'properties', '{}')) AS p
GROUP BY c.kind_id;

-- Every kind's parent and merged properties before this migration moves any kind
-- (step 7a keeps them).
CREATE TEMP TABLE inventory_k1_before ON COMMIT DROP AS
SELECT k.id AS kind_id, k.parent_id, coalesce(p.props, '{}') AS props
FROM item_kinds k LEFT JOIN inventory_k1_props p ON p.kind_id = k.id;

-- 1. seeded_kinds: the roots, the `seed: true` kinds and every phase 2 built-in kind, as
-- kinds.yaml has them. `printer` gets no special type until K6 builds its handlers
-- (R-K1-7). A kind that exists keeps its uom and `saleable`. A person's kind that has one
-- of these ids becomes the built-in kind and is named in the detail with its old name,
-- parent, tracking, life basis and schema; if it has items and its tracking is not the
-- seeded kind's, the migration stops (their stock would change meaning). `tracking` and
-- `life_basis` are set in step 8.
CREATE TEMP TABLE inventory_k1_seed ON COMMIT DROP AS
SELECT * FROM (VALUES
    ('service', 'Service', NULL, 'service', NULL, NULL, NULL, NULL, 'each', '{}', false),
    ('consumable', 'Consumable', NULL, 'physical', 'consumable', NULL, NULL, NULL, 'each', '{}', false),
    ('durable', 'Durable', NULL, 'physical', 'durable', 'unit', NULL, NULL, 'each', '{}', false),
    ('filament', 'Filament', 'consumable', NULL, NULL, 'unit', NULL, 'filament_spool', 'g', '{}', false),
    ('general_consumable', 'General consumable', 'consumable', NULL, NULL, 'lot', NULL, NULL, 'each', '{}', false),
    ('desiccant', 'Desiccant', 'consumable', NULL, NULL, 'lot', NULL, NULL, 'g', '{}', false),
    ('packaging', 'Packaging', 'consumable', NULL, NULL, 'lot', NULL, NULL, 'each', '{}', false),
    ('lubricant', 'Lubricant', 'consumable', NULL, NULL, 'lot', NULL, NULL, 'ml', '{}', false),
    ('hardware', 'Hardware', 'consumable', NULL, NULL, 'lot', NULL, NULL, 'each', '{}', false),
    ('ams_part', 'AMS part', 'consumable', NULL, NULL, 'lot', NULL, NULL, 'each', '{}', false),
    ('printed_part', 'Printed part', 'consumable', NULL, NULL, 'lot', NULL, NULL, 'each', '{}', true),
    ('assembly', 'Assembly', 'consumable', NULL, NULL, 'lot', NULL, NULL, 'each', '{}', true),
    ('printer', 'Printer', 'durable', NULL, NULL, NULL, NULL, NULL, 'each',
     '{"life": {"handler": "life.hours", "params": {"hours": 10000, "source": "own_run_hours"}}}', false),
    ('part', 'Part', 'durable', NULL, NULL, NULL, NULL, NULL, 'each', '{}', false),
    ('nozzle', 'Nozzle', 'part', NULL, NULL, NULL, NULL, NULL, 'each',
     '{"life": {"handler": "life.hours", "params": {"hours": 1000, "source": "container_run_hours", "warn_pct": 90}}}', false),
    ('hotend', 'Hotend', 'part', NULL, NULL, NULL, NULL, NULL, 'each',
     '{"life": {"handler": "life.hours", "params": {"hours": 2000, "source": "container_run_hours"}}}', false),
    ('belt', 'Belt', 'part', NULL, NULL, NULL, NULL, NULL, 'each',
     '{"life": {"handler": "life.hours", "params": {"hours": 3000, "source": "container_run_hours"}}}', false),
    ('build_plate', 'Build plate', 'part', NULL, NULL, NULL, NULL, NULL, 'each',
     '{"life": {"handler": "life.uses", "params": {"uses": 1000, "warn_pct": 90}}}', false),
    ('spool_part', 'Spool insert', 'durable', NULL, NULL, NULL, NULL, NULL, 'each', '{}', false),
    ('equipment', 'Equipment', 'durable', NULL, NULL, NULL, NULL, NULL, 'each',
     '{"life": {"handler": "life.months", "params": {"months": 60}}}', false),
    ('dry_bin', 'Dry bin', 'equipment', NULL, NULL, NULL, NULL, NULL, 'each', '{}', false),
    ('tool', 'Tool', 'equipment', NULL, NULL, NULL, NULL, NULL, 'each', '{}', false),
    ('warranty', 'Warranty', 'service', NULL, NULL, NULL, 'term', NULL, 'each',
     '{"life": {"params": {"warn_days": 30}}}', false),
    ('service_plan', 'Service plan', 'service', NULL, NULL, NULL, 'term', NULL, 'each',
     '{"life": {"params": {"warn_days": 30}}}', false),
    ('labour', 'Labour', 'service', NULL, NULL, NULL, 'hourly', NULL, 'each', '{}', false),
    ('flat_service', 'Flat-rate service', 'service', NULL, NULL, NULL, 'flat', NULL, 'each', '{}', false)
) AS s (id, name, parent_id, nature, role, counting, billing, special_type, uom, steps, saleable);

DO $$
DECLARE
    clash record;
BEGIN
    FOR clash IN
        SELECT k.id, k.tracking,
            CASE WHEN s.nature = 'service' OR s.billing IS NOT NULL THEN 'none'
                ELSE s.counting END AS seeded,
            (SELECT count(*) FROM items i WHERE i.kind_id = k.id) AS items
        FROM inventory_k1_seed s JOIN item_kinds k ON k.id = s.id
        WHERE NOT k.builtin
    LOOP
        IF clash.items > 0 AND clash.tracking IS DISTINCT FROM clash.seeded THEN
            RAISE EXCEPTION 'inventory K1: the person''s kind % has % items tracked by %; '
                'the built-in kind % tracks by %', clash.id, clash.items, clash.tracking,
                clash.id, clash.seeded;
        END IF;
    END LOOP;
END
$$;

INSERT INTO inventory_k1_report (rule, count, detail)
SELECT 'seeded_kinds', count(*),
    coalesce(jsonb_agg(CASE WHEN k.id IS NULL THEN
            jsonb_build_object('kind', s.id, 'was', 'new')
        ELSE
            jsonb_build_object('kind', s.id, 'was', 'a person''s kind', 'name', k.name,
                'parent_id', k.parent_id, 'tracking', k.tracking, 'life_basis', k.life_basis,
                'attribute_schema', k.attribute_schema)
        END ORDER BY s.id)
        FILTER (WHERE k.id IS NULL OR NOT k.builtin), '[]')
FROM inventory_k1_seed s LEFT JOIN item_kinds k ON k.id = s.id;

INSERT INTO item_kinds (id, name, parent_id, tracking, uom, nature, role, counting, billing,
    special_type, steps, saleable, builtin)
SELECT id, name, parent_id, 'none', uom, nature, role, counting, billing, special_type,
    steps::jsonb, saleable, true
FROM inventory_k1_seed
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    parent_id = EXCLUDED.parent_id,
    nature = EXCLUDED.nature,
    role = EXCLUDED.role,
    counting = EXCLUDED.counting,
    billing = EXCLUDED.billing,
    special_type = EXCLUDED.special_type,
    steps = EXCLUDED.steps,
    builtin = true;

-- 2. person_kinds (§8, R-K1-9): each kind a person made gets the class of its `tracking`
-- (`lot`: consumable, lot; `unit`: durable; `none`: service, flat; `filament`: under
-- `filament`), and sets only the keys its parent leaves open. A kind whose class
-- conflicts with its parent's, and a root, move under the root of their class. Parents
-- are placed before their children.
CREATE TEMP TABLE inventory_k1_moved (kind text, old_parent text, new_parent text)
    ON COMMIT DROP;

DO $$
DECLARE
    k record;
    p record;
    t record;
BEGIN
    FOR k IN
        SELECT ik.id, ik.parent_id, ik.tracking
        FROM item_kinds ik
        JOIN (SELECT kind_id, max(depth) AS depth FROM inventory_k1_chain GROUP BY kind_id) d
            ON d.kind_id = ik.id
        WHERE NOT ik.builtin
        ORDER BY d.depth, ik.id
    LOOP
        IF k.tracking = 'filament' THEN
            IF NOT EXISTS (
                SELECT 1 FROM inventory_k1_chain WHERE kind_id = k.id AND anc_id = 'filament'
            ) THEN
                UPDATE item_kinds SET parent_id = 'filament' WHERE id = k.id;
                INSERT INTO inventory_k1_moved VALUES (k.id, k.parent_id, 'filament');
            END IF;
            CONTINUE;
        END IF;
        SELECT * INTO t FROM (VALUES
            ('lot', 'consumable', 'physical', 'consumable', 'lot', NULL),
            ('unit', 'durable', 'physical', 'durable', 'unit', NULL),
            ('none', 'service', 'service', NULL, NULL, 'flat')
        ) AS r (tracking, root, nature, role, counting, billing)
        WHERE r.tracking = k.tracking;
        SELECT * INTO p FROM inventory_k1_class WHERE kind_id = k.parent_id;
        IF k.parent_id IS NULL
            OR p.nature <> t.nature OR p.role <> t.role
            OR p.counting <> t.counting OR p.billing <> t.billing
        THEN
            UPDATE item_kinds SET parent_id = t.root WHERE id = k.id;
            INSERT INTO inventory_k1_moved VALUES (k.id, k.parent_id, t.root);
            SELECT * INTO p FROM inventory_k1_class WHERE kind_id = t.root;
        END IF;
        UPDATE item_kinds SET
            nature = CASE WHEN p.nature IS NULL THEN t.nature END,
            role = CASE WHEN p.role IS NULL THEN t.role END,
            counting = CASE WHEN p.counting IS NULL THEN t.counting END,
            billing = CASE WHEN p.billing IS NULL THEN t.billing END
        WHERE id = k.id;
    END LOOP;
END
$$;

INSERT INTO inventory_k1_report (rule, count, detail)
SELECT 'person_kinds',
    (SELECT count(*) FROM item_kinds WHERE NOT builtin),
    coalesce((SELECT jsonb_agg(jsonb_build_object('kind', kind, 'old_parent', old_parent,
        'new_parent', new_parent) ORDER BY kind) FROM inventory_k1_moved), '[]');

-- 3. kind_life (§8): a person kind's `life_basis` becomes its `steps.life` handler, with
-- no `hours` or `months`, so the kind is incomplete until a person sets them (R-K1-8).
-- Only a durable kind has a life; another kind's basis is dropped and named. This holds
-- under a parent that names the same handler too (a kind under `nozzle` gets
-- `source: own_run_hours`), as §8 says.
CREATE TEMP TABLE inventory_k1_kind_life ON COMMIT DROP AS
SELECT k.id, k.life_basis, c.role, c.life
FROM item_kinds k JOIN inventory_k1_class c ON c.kind_id = k.id
WHERE NOT k.builtin AND k.life_basis <> 'none';

UPDATE item_kinds k SET steps = k.steps || jsonb_build_object('life',
    CASE l.life_basis
        WHEN 'hours' THEN '{"handler": "life.hours", "params": {"source": "own_run_hours"}}'::jsonb
        ELSE '{"handler": "life.months", "params": {}}'::jsonb
    END)
FROM inventory_k1_kind_life l
WHERE l.id = k.id AND l.role = 'durable';

INSERT INTO inventory_k1_report (rule, count, detail)
SELECT 'kind_life', count(*),
    coalesce(jsonb_agg(jsonb_build_object('kind', id, 'life_basis', life_basis) ORDER BY id)
        FILTER (WHERE role IS DISTINCT FROM 'durable'), '[]')
FROM inventory_k1_kind_life;

-- 4. items_on_roots (§8): a root leaves its class open, so no item may sit on it.
WITH moved AS (
    UPDATE items SET kind_id = CASE kind_id
        WHEN 'consumable' THEN 'general_consumable' ELSE 'flat_service' END
    WHERE kind_id IN ('consumable', 'service')
    RETURNING id, kind_id
)
INSERT INTO inventory_k1_report (rule, count, detail)
SELECT 'items_on_roots', count(*),
    coalesce(jsonb_agg(jsonb_build_object('item', id, 'kind', kind_id) ORDER BY id), '[]')
FROM moved;

-- 5. item_life (§4.5): `items.life_hours` and `life_months` move into `steps.life`. On a
-- durable item a value the resolved life handler takes becomes its parameter; else
-- `life_hours` names `life.hours` (`source: own_run_hours`), or `life_months` names
-- `life.months`. A value left over, and every value on an item that is not durable, is
-- dropped and named. The columns stay, unwritten, until K2.
CREATE TEMP TABLE inventory_k1_item_life ON COMMIT DROP AS
SELECT i.id, i.life_hours, i.life_months, c.life,
    CASE WHEN c.role = 'durable' THEN
        CASE
            WHEN i.life_hours IS NOT NULL AND c.life = 'life.hours' THEN 'hours'
            WHEN i.life_months IS NOT NULL AND c.life = 'life.months' THEN 'months'
            WHEN i.life_hours IS NOT NULL THEN 'hours'
            ELSE 'months'
        END
    END AS kept
FROM items i JOIN inventory_k1_class c ON c.kind_id = i.kind_id
WHERE i.life_hours IS NOT NULL OR i.life_months IS NOT NULL;

UPDATE items i SET steps = i.steps || jsonb_build_object('life', CASE
    WHEN l.kept = 'hours' AND l.life = 'life.hours' THEN
        jsonb_build_object('params', jsonb_build_object('hours', trim_scale(l.life_hours)))
    WHEN l.kept = 'hours' THEN
        jsonb_build_object('handler', 'life.hours', 'params',
            jsonb_build_object('hours', trim_scale(l.life_hours), 'source', 'own_run_hours'))
    WHEN l.life = 'life.months' THEN
        jsonb_build_object('params', jsonb_build_object('months', l.life_months))
    ELSE
        jsonb_build_object('handler', 'life.months', 'params',
            jsonb_build_object('months', l.life_months))
    END)
FROM inventory_k1_item_life l
WHERE l.id = i.id AND l.kept IS NOT NULL;

INSERT INTO inventory_k1_report (rule, count, detail)
SELECT 'item_life', (SELECT count(*) FROM inventory_k1_item_life),
    coalesce(jsonb_agg(d ORDER BY d ->> 'item', d ->> 'field'), '[]')
FROM (
    SELECT jsonb_build_object('item', id, 'field', 'life_hours', 'value', trim_scale(life_hours)) AS d
    FROM inventory_k1_item_life
    WHERE life_hours IS NOT NULL AND kept IS DISTINCT FROM 'hours'
    UNION ALL
    SELECT jsonb_build_object('item', id, 'field', 'life_months', 'value', life_months)
    FROM inventory_k1_item_life
    WHERE life_months IS NOT NULL AND kept IS DISTINCT FROM 'months'
) dropped;

-- unit_life: `stock_units.life_*` become `steps.life.params` only where the unit's
-- resolved life handler takes the key (a unit sets parameters only, §4.2 layer 5).
CREATE TEMP TABLE inventory_k1_unit_life ON COMMIT DROP AS
SELECT u.id, u.life_hours, u.life_months,
    coalesce(i.steps -> 'life' ->> 'handler', c.life) AS life
FROM stock_units u
JOIN items i ON i.id = u.item_id
JOIN inventory_k1_class c ON c.kind_id = i.kind_id
WHERE u.life_hours IS NOT NULL OR u.life_months IS NOT NULL;

UPDATE stock_units u SET steps = u.steps || jsonb_build_object('life', jsonb_build_object(
    'params', CASE
        WHEN l.life = 'life.hours' THEN jsonb_build_object('hours', trim_scale(l.life_hours))
        ELSE jsonb_build_object('months', l.life_months)
    END))
FROM inventory_k1_unit_life l
WHERE l.id = u.id AND (
    (l.life = 'life.hours' AND l.life_hours IS NOT NULL)
    OR (l.life = 'life.months' AND l.life_months IS NOT NULL)
);

INSERT INTO inventory_k1_report (rule, count, detail)
SELECT 'unit_life', (SELECT count(*) FROM inventory_k1_unit_life),
    coalesce(jsonb_agg(d ORDER BY d ->> 'unit', d ->> 'field'), '[]')
FROM (
    SELECT jsonb_build_object('unit', id, 'field', 'life_hours', 'value', trim_scale(life_hours)) AS d
    FROM inventory_k1_unit_life
    WHERE life_hours IS NOT NULL AND life IS DISTINCT FROM 'life.hours'
    UNION ALL
    SELECT jsonb_build_object('unit', id, 'field', 'life_months', 'value', life_months)
    FROM inventory_k1_unit_life
    WHERE life_months IS NOT NULL AND life IS DISTINCT FROM 'life.months'
) dropped;

-- 6. part_lots (§8, R-K1-10): a lot of an item whose kind is now durable becomes whole
-- units at the lot's unit cost (`origin = 'opening'`, one `receive` move each), and the
-- lot gets an `adjust` move of what left it. A fraction stays on the lot and is named.
-- Expected to touch no rows on prod (scadbuddy-09, 2026-11-24: no parts in `part`).
CREATE TEMP TABLE inventory_k1_lots ON COMMIT DROP AS
SELECT l.id AS lot_id, l.item_id, l.location_id, l.unit_cost_minor, l.cost_unknown,
    l.received_at, floor(l.on_hand)::bigint AS units, l.on_hand - floor(l.on_hand) AS left_qty
FROM stock_lots l
JOIN items i ON i.id = l.item_id
JOIN inventory_k1_class c ON c.kind_id = i.kind_id
WHERE c.role = 'durable' AND l.on_hand > 0;

WITH made AS (
    INSERT INTO stock_units (item_id, location_id, cost_minor, cost_unknown, origin,
        received_at, created_by)
    SELECT t.item_id, t.location_id, t.unit_cost_minor, t.cost_unknown, 'opening',
        t.received_at, a.actor
    FROM inventory_k1_lots t
    CROSS JOIN LATERAL generate_series(1, t.units) AS n
    CROSS JOIN inventory_k1_actor a
    RETURNING id, item_id, cost_minor, cost_unknown
)
INSERT INTO stock_moves (item_id, unit_id, kind, qty, unit_cost_minor, amount_minor,
    cost_unknown, reason, actor)
SELECT m.item_id, m.id, 'receive', 1, m.cost_minor, m.cost_minor, m.cost_unknown,
    'inventory K1: a durable lot became units', a.actor
FROM made m CROSS JOIN inventory_k1_actor a;

INSERT INTO stock_moves (item_id, lot_id, kind, qty, unit_cost_minor, amount_minor,
    cost_unknown, reason, actor)
SELECT t.item_id, t.lot_id, 'adjust', -t.units, t.unit_cost_minor,
    round(-t.units * t.unit_cost_minor, 6), t.cost_unknown,
    'inventory K1: a durable lot became units', a.actor
FROM inventory_k1_lots t CROSS JOIN inventory_k1_actor a
WHERE t.units > 0;

UPDATE stock_lots l SET on_hand = l.on_hand - t.units
FROM inventory_k1_lots t
WHERE t.lot_id = l.id AND t.units > 0;

INSERT INTO inventory_k1_report (rule, count, detail)
SELECT 'part_lots', count(*),
    coalesce(jsonb_agg(jsonb_build_object('lot', lot_id, 'item', item_id, 'units', units,
        'left', trim_scale(left_qty)) ORDER BY lot_id), '[]')
FROM inventory_k1_lots;

-- 7. model_codes (§8, R-K1-11): each distinct trimmed printer model code in
-- `items.compatible_with` becomes a kind under `printer`, named as the oldest item spells
-- it; codes that give one id share it. Each item gets one `compatible_with` relationship
-- per code kind. A `printer` item with one code kind takes it; one with more stays and is
-- named. A kind that already has the id is kept as it is and named.
CREATE TEMP TABLE inventory_k1_codes ON COMMIT DROP AS
SELECT i.id AS item_id, i.kind_id AS item_kind, btrim(c.code) AS code,
    'printer_' || left(regexp_replace(lower(btrim(c.code)), '[^a-z0-9]+', '_', 'g'), 32) AS kind,
    row_number() OVER (ORDER BY i.created_at, i.id, c.n) AS age
FROM items i
CROSS JOIN LATERAL unnest(i.compatible_with) WITH ORDINALITY AS c (code, n)
WHERE btrim(c.code) <> '';

CREATE TEMP TABLE inventory_k1_model_kinds ON COMMIT DROP AS
SELECT c.kind, left((array_agg(c.code ORDER BY c.age))[1], 100) AS name,
    EXISTS (SELECT 1 FROM item_kinds k WHERE k.id = c.kind) AS existed
FROM inventory_k1_codes c
GROUP BY c.kind;

INSERT INTO item_kinds (id, name, parent_id, tracking, life_basis)
SELECT kind, name, 'printer', 'unit', 'none' FROM inventory_k1_model_kinds WHERE NOT existed;

INSERT INTO entities (kind, ref)
SELECT 'item_kind', kind FROM inventory_k1_model_kinds
UNION
SELECT 'item', item_id::text FROM inventory_k1_codes
ON CONFLICT (kind, ref) DO NOTHING;

INSERT INTO relations (type_id, from_entity, to_entity, created_by)
SELECT DISTINCT 'compatible_with', f.id, t.id, a.actor
FROM inventory_k1_codes c
JOIN entities f ON f.kind = 'item' AND f.ref = c.item_id::text
JOIN entities t ON t.kind = 'item_kind' AND t.ref = c.kind
CROSS JOIN inventory_k1_actor a;

CREATE TEMP TABLE inventory_k1_printers ON COMMIT DROP AS
SELECT item_id, array_agg(DISTINCT kind ORDER BY kind) AS kinds
FROM inventory_k1_codes
WHERE item_kind = 'printer'
GROUP BY item_id;

UPDATE items i SET kind_id = p.kinds[1]
FROM inventory_k1_printers p
JOIN item_kinds k ON k.id = p.kinds[1]
WHERE i.id = p.item_id AND cardinality(p.kinds) = 1 AND k.parent_id = 'printer';

INSERT INTO inventory_k1_report (rule, count, detail)
SELECT 'model_codes', (SELECT count(*) FROM inventory_k1_model_kinds),
    coalesce((SELECT jsonb_agg(d) FROM (
        SELECT jsonb_build_object('kind', kind, 'name', name, 'existed', existed) AS d
        FROM inventory_k1_model_kinds
        UNION ALL
        SELECT jsonb_build_object('stays', item_id, 'kinds', to_jsonb(kinds))
        FROM inventory_k1_printers WHERE cardinality(kinds) > 1
    ) x), '[]');

-- 7a. kind_schemas (§8: every row keeps its meaning): a kind whose parent changed keeps
-- each property its old chain gave it. A key the new chain lacks, or gives another
-- definition, goes into the kind's own schema, so its items keep the same schema.
CREATE TEMP TABLE inventory_k1_schemas ON COMMIT DROP AS
SELECT b.kind_id, b.parent_id AS old_parent, k.parent_id AS new_parent,
    (SELECT coalesce(jsonb_object_agg(o.key, o.value), '{}')
     FROM jsonb_each(b.props) AS o
     WHERE (n.props -> o.key) IS DISTINCT FROM o.value) AS kept
FROM inventory_k1_before b
JOIN item_kinds k ON k.id = b.kind_id
LEFT JOIN inventory_k1_props n ON n.kind_id = b.kind_id
WHERE k.parent_id IS DISTINCT FROM b.parent_id;

UPDATE item_kinds k SET attribute_schema = jsonb_set(k.attribute_schema, '{properties}',
    coalesce(k.attribute_schema -> 'properties', '{}') || s.kept)
FROM inventory_k1_schemas s
WHERE s.kind_id = k.id AND s.kept <> '{}';

INSERT INTO inventory_k1_report (rule, count, detail)
SELECT 'kind_schemas', count(*) FILTER (WHERE kept <> '{}'),
    coalesce(jsonb_agg(jsonb_build_object('kind', kind_id, 'old_parent', old_parent,
        'new_parent', new_parent, 'keys', (SELECT jsonb_agg(key ORDER BY key)
            FROM jsonb_object_keys(kept) AS key)) ORDER BY kind_id)
        FILTER (WHERE kept <> '{}'), '[]')
FROM inventory_k1_schemas;

-- 8. tracking_derived (R-K1-6): `tracking` and `life_basis` follow the class and
-- `steps.life`, so the SQL readers of phase 2 stay right. `tracking` is `filament` for
-- the special type `filament_spool`, `none` for a service, else the counting (`lot` when
-- open); `life_basis` is `hours` for `life.hours`, `months` for `life.months`, else `none`.
WITH derived AS (
    SELECT k.id, k.tracking AS old_tracking, k.life_basis AS old_life_basis,
        CASE
            WHEN c.special_type = 'filament_spool' THEN 'filament'
            WHEN c.nature = 'service' THEN 'none'
            ELSE coalesce(c.counting, 'lot')
        END AS tracking,
        CASE c.life WHEN 'life.hours' THEN 'hours' WHEN 'life.months' THEN 'months' ELSE 'none' END
            AS life_basis
    FROM item_kinds k JOIN inventory_k1_class c ON c.kind_id = k.id
),
changed AS (
    UPDATE item_kinds k SET tracking = d.tracking, life_basis = d.life_basis
    FROM derived d
    WHERE d.id = k.id AND (k.tracking, k.life_basis) IS DISTINCT FROM (d.tracking, d.life_basis)
    RETURNING k.id, d.old_tracking, d.old_life_basis, d.tracking, d.life_basis
)
INSERT INTO inventory_k1_report (rule, count, detail)
SELECT 'tracking_derived', count(*),
    coalesce(jsonb_agg(jsonb_build_object('kind', id,
        'tracking', jsonb_build_array(old_tracking, tracking),
        'life_basis', jsonb_build_array(old_life_basis, life_basis)) ORDER BY id), '[]')
FROM changed;

DROP VIEW inventory_k1_props;
DROP VIEW inventory_k1_class;
DROP VIEW inventory_k1_chain;
