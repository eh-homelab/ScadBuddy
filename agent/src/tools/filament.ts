import { z } from 'zod'
import type { components } from '../api/schema.js'
import { ok } from './call.js'
import { defineTool, idempotencyKey, json, ToolError, type Tool } from './registry.js'

// ScadBuddy's own filament inventory (#6261, epic #6260): the catalogs a spool type or a
// spool picks from, the spool types, and the spools with their tags and ledger. Write
// tier: nothing leaves ScadBuddy at the call, except a tag change's push, which the
// `filament_sync.tags` setting gates. The RFID tools that write Bambuddy itself (the override
// mode, the reconcile, the NTAG write and cancel) are `outward`, gated as the virtual-printer
// tools are (printers.ts), because they change Bambuddy's settings, an AMS slot or a tag.
// Routes: backend/scadbuddy/api/filament.py.

const SOURCE = "ScadBuddy's own filament inventory"

/** A SpoolBuddy device id, as the backend's `DeviceId` accepts it (`filament/models.py`). */
const DEVICE_ID = z.string().regex(/^[A-Za-z0-9._:-]{1,50}$/)

type CsvRow = components['schemas']['CsvRow']

/** The spools CSV's own columns (`filament/csv_import.py` `COLUMNS`, the `CsvRow` schema's keys). */
const CSV_COLUMNS = [
  'number',
  'type',
  'material',
  'subtype',
  'brand',
  'category',
  'effect',
  'colour_name',
  'rgba',
  'extra_colours',
  'diameter_mm',
  'density_g_cm3',
  'nozzle_temp_min',
  'nozzle_temp_max',
  'bed_temp_min',
  'bed_temp_max',
  'label_weight_g',
  'insert_type',
  'low_stock_pct',
  'cost_per_kg',
  'weight_locked',
  'location',
  'note',
] as const satisfies readonly (keyof CsvRow)[]
// Every column of the schema is named above: a new `CsvRow` field fails the typecheck here.
const CSV_COLUMNS_COMPLETE: Exclude<keyof CsvRow, (typeof CSV_COLUMNS)[number]> extends never ? true : never = true
void CSV_COLUMNS_COMPLETE

const kind = z
  .enum([
    'materials',
    'subtypes',
    'brands',
    'categories',
    'effects',
    'insert-materials',
    'insert-types',
    'colours',
    'location-kinds',
  ])
  .describe('Which catalog')

const EVENT_KINDS = [
  'created',
  'loaded',
  'unloaded',
  'moved',
  'print_started',
  'print_ended',
  'weighed',
  'ams_reading',
  'dried',
  'edited',
  'tag_bound',
  'tag_unbound',
  'archived',
  'restored',
  'synced_from_bb',
] as const

const csvRows = z
  .array(z.record(z.string(), z.string().nullable()))
  .min(1)
  .max(5000)
  .describe('One object per CSV row, column name to cell text')

export const filamentTools: Tool[] = [
  defineTool({
    name: 'filament_catalog_list',
    label: 'List a filament catalog',
    description:
      "One of ScadBuddy's filament catalogs, in sort order: materials (print and drying defaults, handling " +
      'flags), subtypes, brands, categories, effects, insert-materials, insert-types, colours (a maker\'s named ' +
      'colours, `#RRGGBBAA`) or location-kinds. Archived rows only with `include_archived`.',
    input: z.object({ kind, include_archived: z.boolean().default(false) }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/catalog/{kind}'],
    handler: async ({ kind, include_archived }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/catalog/{kind}', {
            params: { path: { kind }, query: { include_archived } },
          }),
          `list the ${kind} catalog`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_catalog_upsert',
    label: 'Add or edit a filament catalog row',
    description:
      'Add a row to a filament catalog (no `row_id`), or edit one: only the `fields` given change, and `null` ' +
      'clears a nullable one. Every row has `name` and `sort_order`; the other fields depend on the kind ' +
      '(see filament_catalog_list). `archived: true` takes a row off the lists and frees its name, `false` puts ' +
      'it back. 409 when another unarchived row has the name. Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      kind,
      row_id: z.string().uuid().optional().describe("A row's `id` from filament_catalog_list; omit to add"),
      fields: z.record(z.string(), z.unknown()).default({}).describe('The row fields to set'),
      archived: z.boolean().optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/catalog/{kind}', 'PATCH /api/v1/filament/catalog/{kind}/{row_id}'],
    summarize: ({ kind, row_id, fields }) =>
      row_id ? `Edit ${kind} row ${row_id}` : `Add to ${kind} ${String(fields['name'] ?? '')}`.trim(),
    handler: async ({ kind, row_id, fields, archived }, { backend }) => {
      if (row_id === undefined) {
        if (archived !== undefined) throw new ToolError('archived needs a row_id')
        return json(
          await ok(
            backend.POST('/api/v1/filament/catalog/{kind}', { params: { path: { kind } }, body: fields }),
            `add to the ${kind} catalog`,
          ),
        )
      }
      return json(
        await ok(
          backend.PATCH('/api/v1/filament/catalog/{kind}/{row_id}', {
            params: { path: { kind, row_id } },
            body: archived === undefined ? fields : { ...fields, archived },
          }),
          `edit ${kind} row ${row_id}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'filament_type_list',
    label: 'List spool types',
    description:
      'The spool types, by code. A type is a product: what a maker sells under one code (brand, material, ' +
      'subtype, colour `#RRGGBBAA`, label weight, print values a spool inherits, `cost_per_kg`, `filament_id` of ' +
      'its price book row). Archived types only with `include_archived`.',
    input: z.object({ include_archived: z.boolean().default(false) }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/types'],
    handler: async ({ include_archived }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/types', { params: { query: { include_archived } } }),
          'list the spool types',
        ),
      ),
  }),

  defineTool({
    name: 'filament_type_get',
    label: 'Read a spool type',
    description: "One spool type (archived too) with its slicer presets and pressure-advance (K) values.",
    input: z.object({ type_id: z.string().uuid().describe("A type's `id` from filament_type_list") }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/types/{type_id}'],
    handler: async ({ type_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/types/{type_id}', { params: { path: { type_id } } }),
          `read spool type ${type_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_type_upsert',
    label: 'Add or edit a spool type',
    description:
      'Add a spool type (no `type_id`), or edit one: only the `fields` given change, and `null` clears a ' +
      'nullable one. A new type needs `code`, `material_id`, `rgba` (`#RRGGBB` or `#RRGGBBAA`) and ' +
      '`label_weight_g`; ids come from filament_catalog_list. With a `cost_per_kg` it also keeps the price ' +
      'book row of its material, subtype, brand, colour and alpha in step; a new row takes its stops and effect. ' +
      '`archived: true` takes a type off the lists and frees its code. 409 when an unarchived type has the ' +
      'code; 422 when a minimum temperature is above its maximum. Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      type_id: z.string().uuid().optional().describe("A type's `id`; omit to add"),
      fields: z.record(z.string(), z.unknown()).default({}).describe('The type fields to set'),
      archived: z.boolean().optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/types', 'PATCH /api/v1/filament/types/{type_id}'],
    summarize: ({ type_id, fields }) =>
      type_id ? `Edit spool type ${type_id}` : `Add spool type ${String(fields['code'] ?? '')}`.trim(),
    handler: async ({ type_id, fields, archived }, { backend }) => {
      if (type_id === undefined) {
        if (archived !== undefined) throw new ToolError('archived needs a type_id')
        return json(await ok(backend.POST('/api/v1/filament/types', { body: fields as never }), 'add a spool type'))
      }
      return json(
        await ok(
          backend.PATCH('/api/v1/filament/types/{type_id}', {
            params: { path: { type_id } },
            body: archived === undefined ? fields : { ...fields, archived },
          }),
          `edit spool type ${type_id}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'filament_type_set_presets',
    label: "Set a spool type's slicer presets",
    description:
      "Replace a spool type's slicer filament presets with `presets` (the whole list; an empty list clears " +
      'them): one per printer model and nozzle diameter. Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      type_id: z.string().uuid(),
      presets: z.array(
        z.object({
          printer_model: z.string().describe('A printer model, e.g. `H2C`'),
          nozzle_diameter: z.string().describe('e.g. `0.4`'),
          slicer_filament: z.string().describe("The slicer's filament id, e.g. `GFA00`"),
          slicer_filament_name: z.string().optional(),
        }),
      ),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['PUT /api/v1/filament/types/{type_id}/presets'],
    summarize: ({ type_id, presets }) => `Set ${presets.length} slicer preset(s) on spool type ${type_id}`,
    handler: async ({ type_id, presets }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/filament/types/{type_id}/presets', {
            params: { path: { type_id } },
            body: presets,
          }),
          `set the presets of spool type ${type_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_type_set_k_values',
    label: "Set a spool type's pressure-advance values",
    description:
      "Replace a spool type's pressure-advance (K) values with `k_values` (the whole list; an empty list clears " +
      'them): one per printer, extruder and nozzle diameter. Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      type_id: z.string().uuid(),
      k_values: z.array(
        z.object({
          printer_id: z.string().uuid(),
          extruder: z.number().int().min(0),
          nozzle_diameter: z.string().describe('e.g. `0.4`'),
          k_value: z.string().describe('A decimal, e.g. `0.02`'),
          nozzle_type: z.string().optional(),
          name: z.string().optional(),
          cali_idx: z.number().int().optional(),
          setting_id: z.string().optional(),
        }),
      ),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['PUT /api/v1/filament/types/{type_id}/k-values'],
    summarize: ({ type_id, k_values }) => `Set ${k_values.length} K value(s) on spool type ${type_id}`,
    handler: async ({ type_id, k_values }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/filament/types/{type_id}/k-values', {
            params: { path: { type_id } },
            body: k_values,
          }),
          `set the K values of spool type ${type_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_spool_list',
    label: 'List filament spools',
    description:
      "ScadBuddy's spools (one physical reel each) by number, each with its `effective` values (its own " +
      "override, else its type's), the fields it `overridden`, the `names` behind its ids, `low_stock`, and " +
      '`grams_left` from its ledger. ' +
      '`needs_type: true` lists only spools with no type yet, `false` only typed ones. Archived spools only ' +
      'with `include_archived`.',
    input: z.object({
      include_archived: z.boolean().default(false),
      needs_type: z.boolean().optional(),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/spools'],
    handler: async ({ include_archived, needs_type }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/spools', { params: { query: { include_archived, needs_type } } }),
          'list the spools',
        ),
      ),
  }),

  defineTool({
    name: 'filament_spool_get',
    label: 'Read a spool',
    description:
      'One spool (archived too) with its effective values, `names` (brand, material, subtype, category, effect, ' +
      'insert type and place as text), `low_stock`, and: the `tag` bound now, the `last_tag` it had before with ' +
      '`held_by` (the number of the spool that holds that tag now), its effective `presets` and `k_values`, the ' +
      '`basis` of `grams_left` (the anchor reading and the deltas taken off it), and `in_use` (it has printed, so ' +
      'it can be archived but not deleted), the `invoice` it was bought on (number, date, supplier, line and ' +
      '`unit_cost`; `null` if none), and `type_suppliers` (the unarchived products of its type from unarchived suppliers, each with its ' +
      'newest price and price per kg).',
    input: z.object({ spool_id: z.string().uuid().describe("A spool's `id` from filament_spool_list") }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/spools/{spool_id}'],
    handler: async ({ spool_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/spools/{spool_id}', { params: { path: { spool_id } } }),
          `read spool ${spool_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_spool_upsert',
    label: 'Add or edit a spool',
    description:
      'Add a spool (no `spool_id`), or edit one: only the `fields` given change, and `null` on an override ' +
      "clears it so the spool takes its type's value again. A new spool needs a `type_id` (filament_type_list) " +
      'or, while it needs a type, a `material_id`; `grams_left` is its first reading. Each edit is one `edited` ' +
      'ledger event. To record a weighing use filament_spool_add_event; to archive use filament_spool_archive. ' +
      '422 when an effective minimum temperature is above its maximum or the spool would have neither type nor ' +
      'material. Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      spool_id: z.string().uuid().optional().describe("A spool's `id`; omit to add"),
      fields: z.record(z.string(), z.unknown()).default({}).describe('The spool fields to set'),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools', 'PATCH /api/v1/filament/spools/{spool_id}'],
    summarize: ({ spool_id }) => (spool_id ? `Edit spool ${spool_id}` : 'Add a spool'),
    handler: async ({ spool_id, fields }, { backend }) => {
      if (spool_id === undefined) {
        return json(await ok(backend.POST('/api/v1/filament/spools', { body: fields }), 'add a spool'))
      }
      return json(
        await ok(
          backend.PATCH('/api/v1/filament/spools/{spool_id}', {
            params: { path: { spool_id } },
            body: fields,
          }),
          `edit spool ${spool_id}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'filament_spool_set_presets',
    label: "Set a spool's own slicer presets",
    description:
      "Replace a spool's own slicer filament presets with `presets` (the whole list): one per printer model " +
      "and nozzle diameter. A spool's row for a key overrides its type's; an empty list resets the spool to " +
      "its type's presets. The answer is the effective set (`overridden` marks the spool's own). Only what " +
      'the user says. Writes ScadBuddy only.',
    input: z.object({
      spool_id: z.string().uuid(),
      presets: z.array(
        z.object({
          printer_model: z.string().describe('A printer model, e.g. `H2C`'),
          nozzle_diameter: z.string().describe('e.g. `0.4`'),
          slicer_filament: z.string().describe("The slicer's filament id, e.g. `GFA00`"),
          slicer_filament_name: z.string().optional(),
        }),
      ),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['PUT /api/v1/filament/spools/{spool_id}/presets'],
    summarize: ({ spool_id, presets }) => `Set ${presets.length} own slicer preset(s) on spool ${spool_id}`,
    handler: async ({ spool_id, presets }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/filament/spools/{spool_id}/presets', {
            params: { path: { spool_id } },
            body: presets,
          }),
          `set the presets of spool ${spool_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_spool_set_k_values',
    label: "Set a spool's own pressure-advance values",
    description:
      "Replace a spool's own pressure-advance (K) values with `k_values` (the whole list): one per printer, " +
      "extruder and nozzle diameter. A spool's row for a key overrides its type's; an empty list resets the " +
      "spool to its type's values. The answer is the effective set (`overridden` marks the spool's own). " +
      'Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      spool_id: z.string().uuid(),
      k_values: z.array(
        z.object({
          printer_id: z.string().uuid(),
          extruder: z.number().int().min(0),
          nozzle_diameter: z.string().describe('e.g. `0.4`'),
          k_value: z.string().describe('A decimal, e.g. `0.02`'),
          nozzle_type: z.string().optional(),
          name: z.string().optional(),
          cali_idx: z.number().int().optional(),
          setting_id: z.string().optional(),
        }),
      ),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['PUT /api/v1/filament/spools/{spool_id}/k-values'],
    summarize: ({ spool_id, k_values }) => `Set ${k_values.length} own K value(s) on spool ${spool_id}`,
    handler: async ({ spool_id, k_values }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/filament/spools/{spool_id}/k-values', {
            params: { path: { spool_id } },
            body: k_values,
          }),
          `set the K values of spool ${spool_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_spool_archive',
    label: 'Archive or restore a spool',
    description:
      '`archived: true` takes a spool off the lists (an empty or retired reel); `false` puts it back. A spool ' +
      'that has printed cannot be deleted, only archived. Only what the user says. Writes ScadBuddy; with tag sync on (filament_sync.tags), ' +
      'the archived spool\'s tag is then cleared in Bambuddy.',
    input: z.object({ spool_id: z.string().uuid(), archived: z.boolean().default(true) }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/{spool_id}/archive'],
    summarize: ({ spool_id, archived }) => `${archived ? 'Archive' : 'Restore'} spool ${spool_id}`,
    handler: async ({ spool_id, archived }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/spools/{spool_id}/archive', {
            params: { path: { spool_id } },
            body: { archived },
          }),
          `archive spool ${spool_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_spool_delete',
    label: 'Delete a spool',
    description:
      'Delete a spool that never printed (one added by mistake), with its ledger and tag bindings. 409 when its ' +
      'ledger holds a print or Bambuddy links it (the next pull would make it again): use filament_spool_archive instead. Only when the user asks. Writes ScadBuddy only.',
    input: z.object({ spool_id: z.string().uuid() }),
    risk: 'write',
    source: SOURCE,
    routes: ['DELETE /api/v1/filament/spools/{spool_id}'],
    summarize: ({ spool_id }) => `Delete spool ${spool_id}`,
    handler: async ({ spool_id }, { backend }) => {
      await ok(
        backend.DELETE('/api/v1/filament/spools/{spool_id}', { params: { path: { spool_id } } }),
        `delete spool ${spool_id}`,
      )
      return json({ deleted: spool_id })
    },
  }),

  defineTool({
    name: 'filament_spool_bind_tag',
    label: 'Put a tag on a spool',
    description:
      'Bind an RFID or NFC tag to a spool, by its `tag_uid`, its `tray_uuid`, or both (as a tray reports them). ' +
      'A tag not seen before is made with `kind`. A tag on another spool moves to this one, and a tag this ' +
      "spool had comes off; both spools' ledgers record it. Only what the user says. Writes ScadBuddy; with " +
      'tag sync set to push (`sb_to_bb` or `two_way`) it also tells Bambuddy, for up to 2.5 s, and otherwise the refresh does.',
    input: z.object({
      spool_id: z.string().uuid(),
      tag_uid: z.string().optional(),
      tray_uuid: z.string().optional(),
      kind: z.enum(['bambu_mifare', 'ntag_openspool', 'ntag_opentag', 'other']).default('other'),
      override: z
        .boolean()
        .default(false)
        .describe(
          'true keeps a Bambu tag (a `tray_uuid`, kind `bambu_mifare`; 422 for any other) on a spool of another ' +
            "filament and locks the spool's weight; the AMS slot is then configured for that spool, whatever the override " +
            'mode says. Moving a tag to this spool needs no flag: send false',
        ),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/{spool_id}/tags'],
    summarize: ({ spool_id, tag_uid, tray_uuid }) => `Put tag ${tag_uid ?? tray_uuid ?? ''} on spool ${spool_id}`,
    handler: async ({ spool_id, tag_uid, tray_uuid, kind, override }, { backend }) => {
      if (tag_uid === undefined && tray_uuid === undefined) throw new ToolError('give a tag_uid or a tray_uuid')
      return json(
        await ok(
          backend.POST('/api/v1/filament/spools/{spool_id}/tags', {
            params: { path: { spool_id } },
            body: { tag_uid, tray_uuid, kind, override },
          }),
          `bind a tag to spool ${spool_id}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'filament_tag_unbind',
    label: 'Take a tag off its spool',
    description:
      "Take a tag off the spool it is bound to; the spool's ledger records it. `tag_id` is a binding's " +
      '`tag_id`. 404 when the tag is on no spool. Only what the user says. Writes ScadBuddy and queues the clear for ' +
      'Bambuddy. With tag sync set to push (`sb_to_bb` or `two_way`) it also tells Bambuddy, for up to 2.5 s, and otherwise the refresh does.',
    input: z.object({ tag_id: z.string().uuid() }),
    risk: 'write',
    source: SOURCE,
    routes: ['DELETE /api/v1/filament/tags/{tag_id}/binding'],
    summarize: ({ tag_id }) => `Take tag ${tag_id} off its spool`,
    handler: async ({ tag_id }, { backend }) =>
      json(
        await ok(
          backend.DELETE('/api/v1/filament/tags/{tag_id}/binding', { params: { path: { tag_id } } }),
          `unbind tag ${tag_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_spool_events',
    label: "Read a spool's ledger",
    description:
      'Every event of a spool, oldest first: readings (scale, person, AMS, Bambuddy) in `grams_after`, print ' +
      'use in `delta_g`, edits, tags, drying. `grams_left` is the newest reading less the later use.',
    input: z.object({ spool_id: z.string().uuid() }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/spools/{spool_id}/events'],
    handler: async ({ spool_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/spools/{spool_id}/events', { params: { path: { spool_id } } }),
          `read the ledger of spool ${spool_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_tag_list',
    label: 'List spool tags',
    description:
      "ScadBuddy's spool tags (RFID or NFC), oldest first, each with its open `binding` and the `spool_number` " +
      'it is on. A tag on no spool only with `include_unbound`.',
    input: z.object({ include_unbound: z.boolean().default(false) }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/tags'],
    handler: async ({ include_unbound }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/tags', { params: { query: { include_unbound } } }),
          'list the tags',
        ),
      ),
  }),

  defineTool({
    name: 'filament_tag_get',
    label: 'Read a spool tag',
    description: 'One tag with its open binding and every binding it has had, newest first. 404 for none.',
    input: z.object({ tag_id: z.string().uuid().describe("A tag's `id` from filament_tag_list") }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/tags/{tag_id}'],
    handler: async ({ tag_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/tags/{tag_id}', { params: { path: { tag_id } } }),
          `read tag ${tag_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_ledger_list',
    label: 'Read the filament ledger',
    description:
      "Every spool's events, newest first, each with its `spool_number`. Filter by `spool_id`, `kind`, `source`, " +
      '`since` (an ISO time with its offset, included) and `until` (the same, excluded). A page holds up to `limit` events (at most 500); when ' +
      '`next_cursor` is set, pass it back as `cursor` for the next page.',
    input: z.object({
      spool_id: z.string().uuid().optional(),
      kind: z.enum(EVENT_KINDS).optional().describe('An event kind'),
      source: z.enum(['ams_remain', 'scale', 'print_estimate', 'print_actual', 'person', 'bb']).optional(),
      since: z.string().optional(),
      until: z.string().optional(),
      cursor: z.string().optional(),
      limit: z.number().int().min(1).max(500).default(100),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/events'],
    handler: async (query, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/events', {
            params: { query },
          }),
          'read the ledger',
        ),
      ),
  }),

  defineTool({
    name: 'filament_summary',
    label: 'Summarise the filament inventory',
    description:
      "The unarchived spools in total: their number, grams left, the value of those grams at each spool's cost " +
      'per kg (`null` when none has a cost), the stock by material, how many sit in an AMS tray, how many are ' +
      'low on stock, and how many still need a type.',
    input: z.object({}),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/summary'],
    handler: async (_input, { backend }) =>
      json(await ok(backend.GET('/api/v1/filament/summary'), 'summarise the inventory')),
  }),

  defineTool({
    name: 'filament_rfid_status',
    label: 'Read the RFID override mode',
    description:
      "Whether the RFID override mode is on and where Bambuddy's auto-add of unknown RFID spools stands: " +
      '`applied` (mode on, Bambuddy\'s value is false), `pending` (a write has not landed or Bambuddy is down; ' +
      '`detail` names the error), `blocked` (Bambuddy refused the write because its auth is on) or `off`. ' +
      'Lists the SpoolBuddy devices. Reads Bambuddy; writes nothing.',
    input: z.object({}),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/rfid/status'],
    handler: async (_input, { backend }) =>
      json(await ok(backend.GET('/api/v1/filament/rfid/status'), 'read the RFID override mode')),
  }),

  defineTool({
    name: 'filament_rfid_override_mode',
    label: 'Turn the RFID override mode on or off',
    description:
      "Only what the user says; this turns Bambuddy's auto-add of unknown RFID spools off while on (it writes " +
      "Bambuddy's settings, whatever `filament_sync.tags` says; approved first). The value " +
      'Bambuddy had is saved first and put back when the mode turns off. Answers the status: `blocked` when ' +
      "Bambuddy's auth is on (it refuses the change; the mode stays as asked), `pending` when Bambuddy cannot " +
      'be reached (the spool refresh retries).',
    input: z.object({ enabled: z.boolean().describe('true turns the mode on, false turns it off') }),
    risk: 'outward',
    source: SOURCE,
    routes: ['PUT /api/v1/filament/rfid/override-mode'],
    summarize: ({ enabled }) => `Turn the RFID override mode ${enabled ? 'on' : 'off'}`,
    handler: async ({ enabled }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/filament/rfid/override-mode', { body: { enabled } }),
          'change the RFID override mode',
        ),
      ),
  }),

  defineTool({
    name: 'filament_rfid_reconcile',
    label: 'Reconcile RFID with Bambuddy now',
    description:
      "Run once what the spool refresh runs, in three steps: bring Bambuddy's auto-add of unknown RFID spools to " +
      'the override mode; push the queued tag changes (only with tag sync set to push (`sb_to_bb` or `two_way`)); then configure ' +
      "the AMS slot of each override tag through Bambuddy's slot-configure route (a printer-control write). Each " +
      'step fails alone. Answers the mode status, the tag push counts (`tags`) and the slot counts (`slots`: ' +
      'configured, unchanged, no_preset, no_temps, no_nozzle, failed; null with no Bambuddy). Writes Bambuddy to ' +
      'match ScadBuddy, and ScadBuddy (the queue, the saved auto-add value, the slot rows). Approved first.',
    input: z.object({}),
    risk: 'outward',
    source: SOURCE,
    routes: ['POST /api/v1/filament/rfid/reconcile'],
    summarize: () => 'Reconcile RFID with Bambuddy',
    handler: async (_input, { backend }) =>
      json(await ok(backend.POST('/api/v1/filament/rfid/reconcile'), 'reconcile RFID with Bambuddy')),
  }),

  defineTool({
    name: 'filament_sync_status',
    label: 'Read the Bambuddy sync status',
    description:
      'How the ScadBuddy-to-Bambuddy filament sync stands. `directions` is the stored `filament_sync`: `off`, ' +
      '`sb_to_bb`, `bb_to_sb` or `two_way` for each of `spools`, `weights`, `tags`, `presets`, `suppliers` and ' +
      '`locations`; the sync writes Bambuddy only for a type set to `sb_to_bb` or `two_way`. `last_pass_at` is the ' +
      "last pass, `last_error` its error (null when it worked), `last_ok_at` the last pass with no error (a skipped " +
      "one too), `counts` the last pass's (`written`, `failed`, `deferred`, `conflicts`, `unmapped_k`). `errors` " +
      'lists each item whose last write to Bambuddy failed (`kind`, its ScadBuddy `sb_id`, `error`, `attempts`, ' +
      '`at`), newest first: this is why a spool is not in Bambuddy. `skipped` says why no pass runs now: ' +
      '`no_bambuddy` or `nothing_pushes` (no data type is set to push); else null. Reads ScadBuddy only.',
    input: z.object({}),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/sync/status'],
    handler: async (_input, { backend }) =>
      json(await ok(backend.GET('/api/v1/filament/sync/status'), 'read the Bambuddy sync status')),
  }),

  defineTool({
    name: 'filament_sync_run',
    label: 'Sync with Bambuddy now',
    description:
      'Ask for a full ScadBuddy-to-Bambuddy sync pass now. It runs in the background on the print worker: it reads ' +
      'Bambuddy, then writes it only for the data types `filament_sync` sets to push (`sb_to_bb` or `two_way`); ' +
      'see filament_sync_status for the directions. Answers `{started: true}` once the pass is asked for; read ' +
      'filament_sync_status afterwards for how it went. 409 `no_bambuddy` with no Bambuddy configured, ' +
      '`nothing_pushes` when no data type is set to push. 503 when the pass could not be asked for (the workflow ' +
      'service did not answer; `detail` names the error): nothing started, so try again later. Writes Bambuddy to ' +
      'match ScadBuddy, and ScadBuddy (the links and the sync status). Approved first.',
    input: z.object({}),
    risk: 'outward',
    source: SOURCE,
    routes: ['POST /api/v1/filament/sync/run'],
    summarize: () => 'Sync the filament data with Bambuddy now',
    handler: async (_input, { backend }) =>
      json(await ok(backend.POST('/api/v1/filament/sync/run'), 'sync with Bambuddy')),
  }),

  defineTool({
    name: 'filament_rfid_scan',
    label: 'Wait for a tag at a SpoolBuddy reader',
    description:
      'Wait up to `timeout_s` seconds (1 to 50, default 30) while the user holds a tag to a SpoolBuddy reader, ' +
      'then answer what it is: `status: tag` with `tag_uid`, `tray_uuid`, `tag_type`, its `kind` (from the bound tag, else the Bambuddy record, for a tag Bambuddy knows), the Bambuddy ' +
      'spool it matched (`bambuddy_spool_id`) and the ScadBuddy spool that holds it (`sb_spool`, or null), or ' +
      '`status: timeout`. Ask the user to hold the tag first, then call this. `device_id` (from ' +
      'filament_rfid_status) limits it to one reader. Writes nothing.',
    input: z.object({
      timeout_s: z.number().int().min(1).max(50).default(30),
      device_id: DEVICE_ID.optional().describe("A SpoolBuddy device's `device_id`"),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['POST /api/v1/filament/rfid/scan'],
    handler: async ({ timeout_s, device_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/rfid/scan', { body: { timeout_s, device_id } }),
          'wait for a tag at a SpoolBuddy reader',
        ),
      ),
  }),

  defineTool({
    name: 'filament_spool_ntag_write',
    label: 'Queue an NTAG write for a spool',
    description:
      "Ask the SpoolBuddy `device_id` (from filament_rfid_status) to write an OpenTag3D tag for the spool's " +
      'Bambuddy record; the user then holds a blank tag to the reader. It changes no tag binding: call ' +
      'filament_spool_ntag_confirm afterwards. `warnings` lists what Bambuddy says is missing from the record. ' +
      '409 `no_bambuddy_spool` when the spool is not linked to a Bambuddy spool; `override_tag_open` when its ' +
      'tag was bound with override (send `replace: true` only when the user agrees to replace it). Only what ' +
      'the user says. Writes a tag through Bambuddy; approved first.',
    input: z.object({
      spool_id: z.string().uuid(),
      device_id: DEVICE_ID,
      replace: z.boolean().default(false),
    }),
    risk: 'outward',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/{spool_id}/ntag/write'],
    summarize: ({ spool_id, device_id }) => `Write an NTAG for spool ${spool_id} on ${device_id}`,
    handler: async ({ spool_id, device_id, replace }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/spools/{spool_id}/ntag/write', {
            params: { path: { spool_id } },
            body: { device_id, replace },
          }),
          `queue an NTAG write for spool ${spool_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_spool_ntag_confirm',
    label: 'Bind the NTAG written for a spool',
    description:
      "Read the spool's Bambuddy record after a write. When it holds a new OpenTag3D tag, bind it to the spool " +
      '(`bound: true`, `tag_uid`); otherwise `bound: false` and nothing changes, so call it again once the ' +
      'write has finished. Only what the user says. Writes ScadBuddy; with tag sync set to push (`sb_to_bb` or `two_way`) it also ' +
      'tells Bambuddy of the new binding.',
    input: z.object({ spool_id: z.string().uuid() }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/{spool_id}/ntag/confirm'],
    summarize: ({ spool_id }) => `Bind the written NTAG to spool ${spool_id}`,
    handler: async ({ spool_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/spools/{spool_id}/ntag/confirm', {
            params: { path: { spool_id } },
          }),
          `confirm the NTAG of spool ${spool_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_spool_ntag_cancel',
    label: 'Cancel a queued NTAG write',
    description:
      'Drop the write the SpoolBuddy `device_id` has queued. Bambuddy queues one write per device, so this drops ' +
      'whichever write that device holds, not only this spool\'s; `spool_id` must exist. Only what the user ' +
      'says. Writes Bambuddy and ScadBuddy: it also deletes ScadBuddy\'s own queued-write rows: every row queued on that ' +
      'device, and this spool\'s row only if it was queued on that device or names no device. A later ' +
      '`filament_spool_ntag_confirm` binds nothing for the rows it deleted; ' +
      'approved first.',
    input: z.object({ spool_id: z.string().uuid(), device_id: DEVICE_ID }),
    risk: 'outward',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/{spool_id}/ntag/cancel'],
    summarize: ({ spool_id, device_id }) => `Cancel the NTAG write on ${device_id} for spool ${spool_id}`,
    handler: async ({ spool_id, device_id }, { backend }) => {
      await ok(
        backend.POST('/api/v1/filament/spools/{spool_id}/ntag/cancel', {
          params: { path: { spool_id } },
          body: { device_id },
        }),
        `cancel the NTAG write for spool ${spool_id}`,
      )
      return json({ cancel_requested: device_id })
    },
  }),

  defineTool({
    name: 'filament_spool_add_event',
    label: 'Record a weighing or a drying',
    description:
      'Record on a spool a weighing (`kind: weighed`, `grams` = the filament left, without the empty spool; ' +
      '`grams_left` starts again from it) or a drying (`kind: dried`, `temp_c`, `hours`). Only what the user ' +
      'says. Writes ScadBuddy only.',
    input: z.object({
      spool_id: z.string().uuid(),
      kind: z.enum(['weighed', 'dried']),
      source: z.enum(['person', 'scale']).default('person'),
      grams: z.number().min(0).optional(),
      temp_c: z.number().int().min(0).optional(),
      hours: z.string().optional().describe('A decimal, e.g. `8`'),
      note: z.string().optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/{spool_id}/events'],
    summarize: ({ spool_id, kind, grams }) =>
      kind === 'weighed' ? `Record ${grams ?? '?'} g on spool ${spool_id}` : `Record a drying of spool ${spool_id}`,
    handler: async ({ spool_id, ...body }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/spools/{spool_id}/events', {
            params: { path: { spool_id } },
            body,
          }),
          `record on spool ${spool_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_location_list',
    label: 'List filament locations',
    description:
      'The places spools sit in (room, shelf, bin, dry box, AMS tray), nested under their parents, each level by ' +
      'sort order then name. Each has `spool_count` (unarchived spools in it) and `children`. Archived places only ' +
      'with `include_archived`.',
    input: z.object({ include_archived: z.boolean().default(false) }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/locations'],
    handler: async ({ include_archived }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/locations', { params: { query: { include_archived } } }),
          'list the filament locations',
        ),
      ),
  }),

  defineTool({
    name: 'filament_location_upsert',
    label: 'Add or edit a filament location',
    description:
      'Add a place (no `location_id`), or edit one: only the `fields` given change, and `null` clears a nullable ' +
      'one. A place needs a `name` and a `kind_id` (filament_catalog_list kind `location-kinds`); `parent_id` puts ' +
      'it under another place. `archived: true` takes a place off the tree and frees its name, `false` puts it back. ' +
      '409 when the name is taken under that parent, or the parent would make the place its own ancestor. Only what ' +
      'the user says. Writes ScadBuddy only.',
    input: z.object({
      location_id: z.string().uuid().optional().describe("A place's `id` from filament_location_list; omit to add"),
      fields: z.record(z.string(), z.unknown()).default({}).describe('The place fields to set'),
      archived: z.boolean().optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/locations', 'PATCH /api/v1/filament/locations/{location_id}'],
    summarize: ({ location_id, fields }) =>
      location_id ? `Edit location ${location_id}` : `Add location ${String(fields['name'] ?? '')}`.trim(),
    handler: async ({ location_id, fields, archived }, { backend }) => {
      if (location_id === undefined) {
        if (archived !== undefined) throw new ToolError('archived needs a location_id')
        return json(await ok(backend.POST('/api/v1/filament/locations', { body: fields as never }), 'add a location'))
      }
      return json(
        await ok(
          backend.PATCH('/api/v1/filament/locations/{location_id}', {
            params: { path: { location_id } },
            body: archived === undefined ? fields : { ...fields, archived },
          }),
          `edit location ${location_id}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'filament_spool_move',
    label: 'Move a spool',
    description:
      'Put a spool in a place (`location_id` from filament_location_list); `null` takes it out of every place. ' +
      "The spool's ledger records the move as `{from, to}`. A spool already there is left as it is. 422 for a place " +
      'that does not exist or is archived. Only what the user says. Writes ScadBuddy only.',
    input: z.object({ spool_id: z.string().uuid(), location_id: z.string().uuid().nullable() }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/{spool_id}/move'],
    summarize: ({ spool_id, location_id }) =>
      location_id ? `Move spool ${spool_id} to ${location_id}` : `Take spool ${spool_id} out of its place`,
    handler: async ({ spool_id, location_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/spools/{spool_id}/move', {
            params: { path: { spool_id } },
            body: { location_id },
          }),
          `move spool ${spool_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_spool_import_preview',
    label: 'Preview a spools CSV import',
    description:
      'What filament_spool_import would do with these CSV rows, and nothing written: create, update or error ' +
      'per line, with `[before, after]` changes. Same rows and cell rules as filament_spool_import.',
    input: z.object({ rows: csvRows }),
    risk: 'read',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/import'],
    summarize: ({ rows }) => `Preview a spools import of ${rows.length} rows`,
    handler: async ({ rows }, ctx) =>
      json(
        await ok(
          ctx.backend.POST('/api/v1/filament/spools/import', { body: { rows, dry_run: true } }),
          'preview the spools import',
        ),
      ),
  }),

  defineTool({
    name: 'filament_spool_import',
    label: 'Import spools from CSV rows',
    description:
      'Make or change spools from rows of a spools CSV (cells as text, under the export\'s column names: ' +
      `${CSV_COLUMNS.join(', ')}). A row with a \`number\` changes that spool where a cell differs; an empty cell ` +
      'changes nothing. A row with no `number` makes a spool (it needs a `type` code or a `material`). Material, ' +
      'subtype, brand, category and effect names are added to their catalog when missing; `insert_type`, `type` ' +
      'and `location` must exist (a location is a move). Derived columns (grams_left, last_used_at, ...) are ' +
      'ignored. It previews first and writes only when no line is an error, all rows at once; to see the ' +
      'preview alone, use filament_spool_import_preview. Only what the user says. Writes ScadBuddy only.',
    input: z.object({ rows: csvRows }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/import'],
    summarize: ({ rows }) => `Import ${rows.length} spool rows`,
    handler: async ({ rows }, ctx) => {
      const preview = await ok(
        ctx.backend.POST('/api/v1/filament/spools/import', { body: { rows, dry_run: true } }),
        'preview the spools import',
      )
      const errors = preview.rows.filter((r) => r.action === 'error')
      if (errors.length > 0) {
        throw new ToolError(
          `Nothing was imported: ${errors.map((r) => `line ${r.line}: ${r.error ?? ''}`).join('; ')}`,
        )
      }
      // One key per call: a re-send after a lost answer gets the first result and writes nothing.
      const headers = { 'Idempotency-Key': idempotencyKey(ctx) }
      return json(
        await ok(
          ctx.backend.POST('/api/v1/filament/spools/import', {
            body: { rows, dry_run: false },
            headers,
          }),
          'import the spools',
        ),
      )
    },
  }),

  defineTool({
    name: 'filament_bambuddy_import',
    label: "Import Bambuddy's spools",
    description:
      "Pull Bambuddy's unarchived spools into ScadBuddy's own inventory once (the spool refresh Schedule also " +
      'does this). A linked spool takes what Bambuddy changed since the last pull, unless ScadBuddy changed that ' +
      'field later. A Bambuddy spool whose tag or record a ScadBuddy spool already holds is linked. Any other ' +
      'becomes a new spool, typed by `material_number`, else by brand, material, subtype, colour and label weight, ' +
      'else left needing a type. An archived ScadBuddy spool is never restored or changed (listed as `archived`). ' +
      'Returns the counts and ids created, linked, updated, still needing a type and archived, and the Bambuddy ' +
      'rows it could not read (`invalid`). ' +
      '409 when no Bambuddy is configured. Reads Bambuddy, writes ScadBuddy only.',
    input: z.object({}),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/bambuddy/import'],
    summarize: () => "Import Bambuddy's spools",
    handler: async (_input, { backend }) =>
      json(await ok(backend.POST('/api/v1/filament/bambuddy/import'), "import Bambuddy's spools")),
  }),

  defineTool({
    name: 'filament_sync_ams_weights',
    label: 'Sync spool weights from the AMS',
    description:
      "Book the AMS's remaining percent of each spool in a tray now as its grams left. It skips locked spools, " +
      'every spool of a printer that is printing, and printers with no fresh report. Only RFID trays carry a remain ' +
      '%, so other trays are counted as `no_remain`. Returns the counts (`booked`, `locked`, `printing`, ' +
      '`no_remain`, `stale`) and the `spools` booked. Writes ScadBuddy only.',
    input: z.object({}),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/sync-ams-weights'],
    summarize: () => 'Sync spool weights from the AMS',
    handler: async (_input, { backend }) =>
      json(await ok(backend.POST('/api/v1/filament/spools/sync-ams-weights'), 'sync the AMS weights')),
  }),

  defineTool({
    name: 'filament_sensors_list',
    label: 'List location sensors',
    description:
      'The humidity and temperature sensors of the places, by name, or of one place with `location_id`. Each has ' +
      'its last value and time, `reachable` and `alerting` (the value is outside its alert band).',
    input: z.object({ location_id: z.string().uuid().optional().describe("A place's `id` from filament_location_list") }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/sensors'],
    handler: async ({ location_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/sensors', { params: { query: { location_id } } }),
          'list the location sensors',
        ),
      ),
  }),

  defineTool({
    name: 'filament_sensor_readings',
    label: "Read a sensor's history",
    description:
      "A sensor's stored readings (`at`, `value`), oldest first, from `since` (default the last 24 hours). With " +
      'more than `limit` readings (default 500, at most 5000), it gives the newest ones.',
    input: z.object({
      sensor_id: z.string().uuid(),
      since: z.string().datetime({ offset: true }).optional().describe('An ISO time with a zone'),
      limit: z.number().int().min(1).max(5000).optional(),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/sensors/{sensor_id}/readings'],
    handler: async ({ sensor_id, since, limit }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/sensors/{sensor_id}/readings', {
            params: { path: { sensor_id }, query: { since, limit } },
          }),
          `read the readings of sensor ${sensor_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_sensor_save',
    label: 'Add or edit a location sensor',
    description:
      'Add a Home Assistant sensor to a place (no `sensor_id`): `fields` needs `location_id`, `source: ' +
      "home_assistant`, `kind` (`humidity` or `temperature`), `entity_id` (from filament_ha_entities) and `name`. " +
      'Or edit one: only the `fields` given change (`location_id`, `name`, `unit`, `alert_below`, `alert_above`, ' +
      '`notify`), and `null` clears a nullable one. An AMS sensor cannot be made by hand: ScadBuddy makes it from ' +
      'the printer report. Alerts are shown, not sent. Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      sensor_id: z.string().uuid().optional().describe("A sensor's `id` from filament_sensors_list; omit to add"),
      fields: z.record(z.string(), z.unknown()).default({}).describe('The sensor fields to set'),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/sensors', 'PATCH /api/v1/filament/sensors/{sensor_id}'],
    summarize: ({ sensor_id, fields }) =>
      sensor_id ? `Edit sensor ${sensor_id}` : `Add sensor ${String(fields['name'] ?? '')}`.trim(),
    handler: async ({ sensor_id, fields }, { backend }) => {
      if (sensor_id === undefined) {
        return json(await ok(backend.POST('/api/v1/filament/sensors', { body: fields as never }), 'add a sensor'))
      }
      return json(
        await ok(
          backend.PATCH('/api/v1/filament/sensors/{sensor_id}', {
            params: { path: { sensor_id } },
            body: fields as never,
          }),
          `edit sensor ${sensor_id}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'filament_sensor_archive',
    label: 'Archive a location sensor',
    description:
      'Take a sensor off the lists. Its readings are kept. 404 when the sensor is missing or already archived. ' +
      'Only what the user says. Writes ScadBuddy only.',
    input: z.object({ sensor_id: z.string().uuid() }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/sensors/{sensor_id}/archive'],
    summarize: ({ sensor_id }) => `Archive sensor ${sensor_id}`,
    handler: async ({ sensor_id }, { backend }) => {
      await ok(
        backend.POST('/api/v1/filament/sensors/{sensor_id}/archive', { params: { path: { sensor_id } } }),
        `archive sensor ${sensor_id}`,
      )
      return json({ archived: sensor_id })
    },
  }),

  defineTool({
    name: 'filament_ha_entities',
    label: 'List Home Assistant entities',
    description:
      'The Home Assistant entities Bambuddy can read, with their states, for choosing a sensor `entity_id`; ' +
      '`search` narrows them. 409 when no Bambuddy is configured. Reads Bambuddy, writes nothing.',
    input: z.object({ search: z.string().optional() }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/sensors/entities'],
    handler: async ({ search }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/sensors/entities', { params: { query: { search } } }),
          'list the Home Assistant entities',
        ),
      ),
  }),
]
