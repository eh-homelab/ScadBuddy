import { createHash } from 'node:crypto'
import { z } from 'zod'
import type { components } from '../api/schema.js'
import { ok } from './call.js'
import { command } from './command.js'
import { linkingTools, type LinkKind } from './filamentLinks.js'
import {
  catalogNames,
  matchesSearch,
  orElse,
  parseSearch,
  SEARCH_ROUTES,
  spoolSearchDoc,
  spoolTags,
  spoolTrays,
  typeSearchDoc,
  typeSkus,
} from './filamentSearch.js'
import { defineTool, idempotencyKey, json, ToolError, type Tool } from './registry.js'

// ScadBuddy's own filament inventory (#6261, epic #6260): the catalogs a spool type or a
// spool picks from, the spool types, and the spools with their tags and ledger. Write
// tier: nothing leaves ScadBuddy at the call, except a tag change's push, which the
// `filament_sync.tags` setting gates. The RFID tools that write Bambuddy itself (the override
// mode, the reconcile, the NTAG write and cancel) are `outward`, gated as the virtual-printer
// tools are (printers.ts), because they change Bambuddy's settings, an AMS slot or a tag.
// Routes: backend/scadbuddy/api/filament.py.

const SOURCE = "ScadBuddy's own filament inventory"

type SpoolCreate = components['schemas']['SpoolCreate-Input']
type SpoolDraft = components['schemas']['SpoolDraft']
type SpoolBulkCreate = components['schemas']['SpoolBulkCreate']
type SpoolBulkUpdate = components['schemas']['SpoolBulkUpdate']

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

/** #6706 — the catalogs whose rows have aliases (backend `filament/catalog.py` `NameTable`). */
const aliasedKind = z.enum(['materials', 'subtypes', 'brands', 'categories', 'effects']).describe('Which catalog')

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
  'consumed_reset',
] as const

/** A calendar day, as the metrics routes' `date` query takes it. */
const DATE = z
  .string()
  .regex(/^\d{4}-\d{2}-\d{2}$/)
  .refine((v) => {
    const d = new Date(`${v}T00:00:00Z`)
    return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === v
  }, 'Not a real calendar date')
  .describe('A date, YYYY-MM-DD')

/** #7152: a money amount as `purchasing_models.Money` takes it: at most 8 whole digits and 4 places. */
const MONEY = z.string().regex(/^\d{1,8}(\.\d{1,4})?$/, 'a decimal string, e.g. "21.40"')

/** The events `filament_spool_history` returns: the route sends them newest first. */
const HISTORY_EVENTS = 50

/** #6894: the Filament page's text search (`filamentSearch.ts`). */
const searchQuery = z
  .string()
  .max(200)
  .optional()
  .describe(
    'Text search, as the Filament page searches: every word must match some field, in any case. `#87` is spool 87 ' +
      'only; `87` is spool 87 or any field holding 87; a hex matches with or without `#` (`#FF6A13`, `ff6a13`).',
  )

const csvRows = z
  .array(z.record(z.string(), z.string().nullable()))
  .min(1)
  .max(5000)
  .describe('One object per CSV row, column name to cell text')

const tools: Tool[] = [
  defineTool({
    name: 'filament_catalog_list',
    label: 'List a filament catalog',
    description:
      "One of ScadBuddy's filament catalogs, in sort order: materials (print and drying defaults, handling " +
      'flags), subtypes, brands, categories, effects, insert-materials, insert-types, colours (a maker\'s named ' +
      'colours, `#RRGGBBAA`) or location-kinds. Archived rows only with `include_archived`. A material, ' +
      'subtype, brand, category or effect has `aliases`: its other names (filament_catalog_set_aliases). The ' +
      "colours catalog holds thousands of rows once synced (filament_colour_sync): pass `q` (every word must match a " +
      "row's name, maker, `material_name` or `#RRGGBBAA`, e.g. `bambu jade white`) rather than listing it whole. A " +
      'colour with `source` came from a sync; one with `source` and a null `synced_at` was edited by a person since (syncs leave it as it is).',
    input: z.object({
      kind,
      include_archived: z.boolean().default(false),
      q: z.string().max(200).optional().describe('Text search: every word must match some text of the row, in any case'),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/catalog/{kind}'],
    handler: async ({ kind, include_archived, q }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/catalog/{kind}', {
            params: { path: { kind }, query: { include_archived, q } },
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
      '(see filament_catalog_list). `archived: true` takes a row off the lists, `false` puts it back. In the ' +
      'materials, subtypes, brands, categories and effects catalogs a name is matched by its key (letters and ' +
      'digits, any case: `dual-color` is `Dual Color`) against every name and alias, archived rows included (a ' +
      "subtype's within its material); the 409 names the row that holds it. In the other catalogs an archived " +
      'row frees its name and a 409 means another unarchived row has it. Only what the user says. Writes ' +
      'ScadBuddy only.',
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
    name: 'filament_catalog_resolve',
    label: 'Find a filament catalog row by name',
    description:
      'The material, subtype, brand, category or effect that a name resolves to, as the Bambuddy and CSV ' +
      "imports resolve it: by key (letters and digits, any case) against every name and alias, archived rows " +
      "too. A subtype is `material_id`'s own, else one for every material. A 404 means no row has that name.",
    input: z.object({
      kind: aliasedKind,
      name: z.string().min(1),
      material_id: z.string().uuid().optional().describe("For a subtype: the material's `id`"),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/catalog/{kind}/resolve'],
    handler: async ({ kind, name, material_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/catalog/{kind}/resolve', {
            params: { path: { kind }, query: { name, material_id } },
          }),
          `find ${name} in the ${kind} catalog`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_catalog_set_aliases',
    label: 'Set the aliases of a filament catalog row',
    description:
      "Replace a material's, subtype's, brand's, category's or effect's aliases (its other names, which the " +
      'imports and the price book resolve to it) with `aliases`, the whole list; `[]` removes them all. Each ' +
      "alias's key (letters and digits, any case) must be free in the catalog: a 409 names the row that holds " +
      'it, and nothing changes. Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      kind: aliasedKind,
      row_id: z.string().uuid().describe("A row's `id` from filament_catalog_list"),
      aliases: z.array(z.string().trim().min(1).max(128)).max(64),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['PUT /api/v1/filament/catalog/{kind}/{row_id}/aliases'],
    summarize: ({ kind, row_id, aliases }) => `Set ${kind} row ${row_id} aliases: ${aliases.join(', ') || 'none'}`,
    handler: async ({ kind, row_id, aliases }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/filament/catalog/{kind}/{row_id}/aliases', {
            params: { path: { kind, row_id } },
            body: { aliases },
          }),
          `set the aliases of ${kind} row ${row_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_colour_sync',
    label: 'Fill the colour catalog',
    description:
      "Fill ScadBuddy's colour catalog from a public source (#6907). `filamentcolors`: every swatch of " +
      'filamentcolors.xyz (maker, name, material, hex; a translucent swatch gets alpha `80`), skipped when the site has ' +
      "no new swatch since the last run unless `force`. `bambu`: Bambu Studio's colour codes as Bambu Lab's colours, " +
      'exact RGBA (`80` for a translucent one). A colour is added, or updated by its maker, name and material; a row ' +
      'a person made or edited, or another source wrote, is left as it is (`kept`), and nothing is deleted. Answers the run: `state` `done`, ' +
      '`unchanged` or `failed` (`error` says why, e.g. the site did not answer; what it wrote stays) with its counts. ' +
      'A filamentcolors run reads about 23 pages: an answer with `status: running` is still running; follow it with ' +
      'get_operation, or read its progress with filament_colour_sync_status. 409 while a run of that source runs. ' +
      'Only when the user asks. Reads the source only and writes ScadBuddy only.',
    input: z.object({
      source: z.enum(['filamentcolors', 'bambu']).describe('Which source to read'),
      force: z.boolean().default(false).describe('filamentcolors only: read every page even when the site has no new swatch'),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/colour-syncs'],
    keyed: true,
    summarize: ({ source }) => `Fill the colour catalog from ${source === 'bambu' ? "Bambu's colour codes" : 'filamentcolors.xyz'}`,
    handler: async ({ source, force }, ctx) =>
      json(
        await command(ctx, `sync the colour catalog from ${source}`, (headers) =>
          ctx.backend.POST('/api/v1/filament/colour-syncs', { body: { source, force }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'filament_colour_sync_status',
    label: "Read the colour catalog's syncs",
    description:
      'The newest run of each colour sync (filament_colour_sync): `state` `running` (with `pages_read` of `pages_total` ' +
      'and the counts so far), `done`, `unchanged` or `failed` with `error`. A source never run is absent.',
    input: z.object({}),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/colour-syncs'],
    handler: async (_, { backend }) =>
      json(await ok(backend.GET('/api/v1/filament/colour-syncs'), "read the colour catalog's syncs")),
  }),

  defineTool({
    name: 'filament_type_list',
    label: 'List spool types',
    description:
      'The spool types, by code. A type is a product: what a maker sells under one code (brand, material, ' +
      'subtype, colour `#RRGGBBAA`, label weight, print values a spool inherits, `cost_per_kg`, `filament_id` of ' +
      'its price book row). Archived types only with `include_archived`. `q` keeps the types whose code, brand, ' +
      'material, subtype, colour name, hex or supplier SKU match it.',
    input: z.object({ include_archived: z.boolean().default(false), q: searchQuery }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/types', 'GET /api/v1/filament/catalog/{kind}', ...SEARCH_ROUTES],
    handler: async ({ include_archived, q }, { backend }) => {
      const types = await ok(
        backend.GET('/api/v1/filament/types', { params: { query: { include_archived } } }),
        'list the spool types',
      )
      const tokens = parseSearch(q)
      if (!tokens.length) return json(types)
      const [names, skus] = await Promise.all([catalogNames(backend), orElse(typeSkus(backend), new Map<string, string[]>())])
      return json(types.filter((type) => matchesSearch(typeSearchDoc(type, names, skus.get(type.id)), tokens)))
    },
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
      '`lead_time_days` and `safety_margin_days` (days, `null` clears) set the type\'s own reorder timing. ' +
      '`archived: true` takes a type off the lists and frees its code. `tdfp_id` (a positive integer below 2^31, `null` ' +
      "clears it) is the type's filament id on 3dfilamentprofiles.com, which filament_type_fetch_profiles " +
      "fetches its slicer presets by. `drying_temp_c` (whole °C) and `drying_hours` (a decimal) are the type's " +
      "drying values, the default of a drying of its spools (filament_spool_add_event). 409 when an unarchived type has the " +
      'code; 422 when a minimum temperature is above its maximum. Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      type_id: z.string().uuid().optional().describe("A type's `id`; omit to add"),
      fields: z
        .object({
          tdfp_id: z
            .number()
            .int()
            .min(1)
            .max(2147483647)
            .nullable()
            .optional()
            .describe("The type's filament id on 3dfilamentprofiles.com: a positive integer below 2^31, null clears it"),
          drying_temp_c: z
            .number()
            .int()
            .min(0)
            .max(1000)
            .nullable()
            .optional()
            .describe('The drying temperature in °C a drying of its spools defaults to; null clears it'),
          drying_hours: z
            .string()
            .nullable()
            .optional()
            .describe('The drying time in hours a drying of its spools defaults to, a decimal such as `8`; null clears it'),
        })
        .catchall(z.unknown())
        .default({})
        .describe('The type fields to set'),
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
    name: 'filament_type_proposals',
    label: 'Propose spool types for spools that need one',
    description:
      'The spool types ScadBuddy proposes for the unarchived spools with no type (spec §8.1), grouped by brand, ' +
      'material, subtype, RGBA, label weight and the `material_number` of their last Bambuddy reading. Each group ' +
      'has a proposed `code`, its catalog names, `colour_name`, its `spool_ids`, `numbers` and `count`, and ' +
      '`type_id` when a type matches it now (confirming uses that type). `ungrouped` lists the spools that lack a ' +
      'material, an RGBA or a label weight, with what each lacks. Show the groups to the user before ' +
      'filament_type_proposals_confirm. Writes nothing.',
    input: z.object({}),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/type-proposals'],
    handler: async (_input, { backend }) =>
      json(await ok(backend.GET('/api/v1/filament/type-proposals'), 'list the type proposals')),
  }),

  defineTool({
    name: 'filament_type_proposals_confirm',
    label: 'Make proposed spool types',
    description:
      "Make the spool types of the groups given (from filament_type_proposals: each group's key fields and " +
      "`spool_ids` as listed, and a `code`: the proposed one or the user's) and type their spools, in one " +
      'transaction. A type that matches a group now is used instead of a new one. Each spool that is still ' +
      "unarchived, untyped and of the group gets the type and loses each own value equal to the type's; any other " +
      'is listed in `skipped` with its reason. Per group, `made` says it made the type and `new` that this call ' +
      'made it: `new` without `made` means the group joined the type an earlier group just made, so its own ' +
      '`code` was not used; tell the user. 409 when a type already has one of the codes (nothing is written). ' +
      'Call it only after the user says yes to these groups and names. Writes ScadBuddy only.',
    input: z.object({
      groups: z
        .array(
          z.object({
            brand_id: z.string().uuid().nullable().optional(),
            material_id: z.string().uuid(),
            subtype_id: z.string().uuid().nullable().optional(),
            rgba: z.string().describe('`#RRGGBBAA`, as the proposal lists it'),
            label_weight_g: z.number().int().positive().max(100_000),
            material_number: z.string().max(128).nullable().optional(),
            code: z.string().min(1).max(128).describe("The new type's code: the proposed one, or the user's"),
            spool_ids: z.array(z.string().uuid()).min(1).max(5000),
          }),
        )
        .min(1)
        .max(500),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/type-proposals/confirm'],
    summarize: ({ groups }) => {
      const spools = groups.reduce((sum, group) => sum + group.spool_ids.length, 0)
      return `Make ${groups.length} spool type${groups.length === 1 ? '' : 's'} for ${spools} spool${spools === 1 ? '' : 's'}`
    },
    handler: async ({ groups }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/type-proposals/confirm', { body: { groups } }),
          'make the proposed spool types',
        ),
      ),
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
          slicer_filament: z.string().describe("The slicer's filament id, e.g. `GFA00`, or a ScadBuddy preset as `sb-<id>`"),
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
    name: 'filament_type_fetch_profiles',
    label: "Fetch a spool type's 3DFP presets",
    description:
      "Start a fetch of a spool type's slicer presets from 3dfilamentprofiles.com by its `tdfp_id`: " +
      "ScadBuddy's profile-fetch worker exports one Bambu Studio preset per printer model ScadBuddy's printers " +
      'have and per nozzle in the `profile_fetch_nozzles` setting; ScadBuddy imports them into Bambuddy\'s ' +
      "local presets and makes them the type's presets for those keys (its other presets stay), with K values " +
      'where a printer has an exact match, never over a K value a person, the fill or a printer calibration set. Answers at once with the run (`run.id`, `run.status` `running`) and ' +
      '`joined`: true when a fetch of the type was already running, which it joins instead of starting another. ' +
      'Follow it with filament_type_profile_fetch. 422 `no_tdfp_id` when the type has no `tdfp_id` ' +
      '(set it with filament_type_upsert), 422 `no_printer_models` when no printer has a model, 503 when the ' +
      'fetch could not be started. Writes ScadBuddy and Bambuddy\'s local presets. Approved first.',
    input: z.object({ type_id: z.string().uuid().describe("A type's `id` from filament_type_list") }),
    risk: 'outward',
    source: SOURCE,
    routes: ['POST /api/v1/filament/types/{type_id}/profile-fetch'],
    summarize: ({ type_id }) => `Fetch the 3DFP presets of spool type ${type_id}`,
    handler: async ({ type_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/types/{type_id}/profile-fetch', { params: { path: { type_id } } }),
          `start the 3DFP preset fetch of spool type ${type_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_type_profile_fetch',
    label: "Read a spool type's 3DFP preset fetch",
    description:
      "A spool type's last preset fetch from 3dfilamentprofiles.com (`run`, null when it never had one): its " +
      '`status` (`running`, `imported`, `partial`, `failed`), the keys it could not set (`missing`, each with ' +
      'its reason), its notes, K values set and `error`. `gaps` lists each printer model and nozzle a fetch ' +
      'would export now that the type has no preset for, with the reason its last fetch gave. A run no ' +
      'profile-fetch worker serves is ended `failed` on this read, and its workflow cancelled, so this read ' +
      'can write. Writes ScadBuddy only.',
    input: z.object({ type_id: z.string().uuid().describe("A type's `id` from filament_type_list") }),
    risk: 'write',
    source: SOURCE,
    routes: ['GET /api/v1/filament/types/{type_id}/profile-fetch'],
    handler: async ({ type_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/types/{type_id}/profile-fetch', { params: { path: { type_id } } }),
          `read the 3DFP preset fetch of spool type ${type_id}`,
        ),
      ),
  }),

  // #7025: 3DFP search and create run on the profile-fetch worker, the one process with the
  // site's browser and login (backend `filament/tdfp.py`).
  defineTool({
    name: 'filament_tdfp_search',
    label: 'Search 3dfilamentprofiles.com',
    description:
      "Find a filament's 3dfilamentprofiles.com (3DFP) id, for a spool type's `tdfp_id`. ScadBuddy's profile-fetch " +
      "worker searches the site's filament list and answers its first page: each candidate's `tdfp_id`, `brand`, " +
      '`material`, `material_type`, colour `name`, `hexes` and `nominal_weight_g` (from a "(250g)" suffix on the ' +
      "name; null when it has none). The site filters only by text, sent as the brand, the material type and the " +
      'colour (`query`); the `material` ranks the candidates (its matches first, then the type\'s) and filters ' +
      'nothing, so check each candidate\'s brand, material and type before you take its id. The site misses some ' +
      'brands: no candidate is not proof that 3DFP lacks the filament. Use it in place of a web search for 3DFP ' +
      'ids. 503 `no_worker` or `not_started` when the worker is not running or is busy with other searches (try ' +
      'again later), 502 when the site failed. Reads only.',
    input: z.object({
      brand: z.string().min(1).max(200).describe('The brand as 3DFP files it, e.g. `SUNLU`'),
      material: z.string().min(1).max(200).optional().describe('e.g. `PLA+/Pro`'),
      material_type: z.string().min(1).max(200).optional().describe('e.g. `Silk`, `Basic`, `Matte`'),
      colour: z.string().min(1).max(200).optional().describe('The colour name, e.g. `Bronze`'),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['POST /api/v1/filament/tdfp/search'],
    handler: async (body, { backend }) =>
      json(await ok(backend.POST('/api/v1/filament/tdfp/search', { body }), 'search 3dfilamentprofiles.com')),
  }),

  defineTool({
    name: 'filament_tdfp_create',
    label: 'Add a filament to 3dfilamentprofiles.com',
    description:
      'Add a filament that 3dfilamentprofiles.com (3DFP) lacks, through its add form, on the profile-fetch ' +
      'worker. Search with filament_tdfp_search first: create only when no candidate is the filament. The form ' +
      'gets `brand`, `material` and `material_type` (each must be an entry the form offers, else 422 ' +
      '`option_missing`; a combination with no site defaults is refused, 422 `no_defaults`), the colour name, ' +
      '`hexes` (1 to 4), `nominal_weight_g`, and the identifiers `mpn`, `gtin`, `asin`, `manufacturer_url`, ' +
      '`empty_spool_weight_g` and `td` where the form has a field for them (`sent`, `not_sent`; ScadBuddy stores ' +
      'none of them). Before it saves, it reads the form\'s duplicate list: one there of the same brand, material ' +
      'and type with a close name or colour stops it with `create.status` `duplicate`, the list and why ' +
      '(`likely`), and nothing is saved; show them to the user, and only when the user says those are other ' +
      'filaments, call again with their ids in `not_duplicates`. Else it answers `created` with the new ' +
      "`tdfp_id`, which becomes `type_id`'s `tdfp_id` when you send one (409 `type_has_tdfp_id` when that type " +
      'already has one). Tried once: 502 `not_created` means it may exist, so search again before another create. ' +
      'Writes 3dfilamentprofiles.com, a shared public database, and the type. Approved first.',
    input: z.object({
      brand: z.string().min(1).max(200),
      material: z.string().min(1).max(200),
      material_type: z.string().min(1).max(200),
      colour: z.string().min(1).max(200).describe('The colour name, e.g. `Bronze`'),
      hexes: z
        .array(z.string().regex(/^#[0-9A-Fa-f]{6}$/))
        .min(1)
        .max(4)
        .describe('One `#RRGGBB` per colour of the filament, the first the main one'),
      nominal_weight_g: z.number().int().positive().max(100000).optional().describe('The net filament weight'),
      mpn: z.string().min(1).max(200).optional().describe("The maker's part number (SKU)"),
      gtin: z.string().regex(/^[0-9]{8,14}$/).optional().describe('The UPC or EAN barcode'),
      asin: z.string().regex(/^[A-Z0-9]{10}$/).optional(),
      manufacturer_url: z.string().url().max(1000).optional().describe("The maker's product page"),
      empty_spool_weight_g: z.number().int().positive().max(10000).optional(),
      td: z.number().positive().max(100).optional().describe('HueForge transmission distance'),
      not_duplicates: z
        .array(z.number().int().positive())
        .max(25)
        .optional()
        .describe('3DFP ids from a `duplicate` answer that the user said are other filaments'),
      type_id: z.string().uuid().optional().describe('The spool type whose `tdfp_id` the new id becomes'),
    }),
    risk: 'outward',
    source: SOURCE,
    routes: ['POST /api/v1/filament/tdfp/filaments'],
    summarize: ({ brand, material, material_type, colour }) =>
      `Add ${brand} ${material} ${material_type} ${colour} to 3dfilamentprofiles.com`,
    handler: async (body, ctx) =>
      json(
        await ok(
          ctx.backend.POST('/api/v1/filament/tdfp/filaments', {
            body,
            // One key per call: a re-send after a lost answer gets the first create's outcome.
            headers: { 'Idempotency-Key': idempotencyKey(ctx) },
          }),
          'add the filament to 3dfilamentprofiles.com',
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
    name: 'filament_type_fill_from_profile',
    label: 'Fill spool types from their slicer profiles',
    description:
      "Fill a spool type's empty print values from its slicer filament profile (#6714): with `type_id` one type, " +
      'without it every unarchived type. The profile is the type preset for the H2C 0.4 mm nozzle, else any 0.4 mm ' +
      'one, else the first: a numeric id is a Bambuddy local preset, any other a Bambu Studio preset. It fills only ' +
      'empty fields (nozzle and bed temperature ranges from the H2C plates, density, drying temperature and hours ' +
      "from the AMS HT's idle drying values, above an AMS 2 Pro's 65 °C for some; a drying value the type's subtype or " +
      "material already sets is left empty, and only a refresh offers it; the diameter is never empty), never the cost (`cost_hint` is only a " +
      'hint). A type with no K values takes those of its linked Bambuddy spools; then each Bambuddy-linked printer of a ' +
      "preset's model gives its K calibrations for the preset's nozzle, and one whose `filament_id` is the preset's " +
      '`slicer_filament` fills that printer, extruder and nozzle when the type has no K value there, or replaces an ' +
      'earlier printer calibration there that differs (a K value a person, the fill or the 3DFP fetch set, its `source`, ' +
      'is never overwritten; `k_reason` says what was not imported). Each result names the fields `filled`, the ' +
      '`source` preset, and a `reason` when the profile could not be read. `refresh: true` answers the profile values that differ from the type (`filled`, with `previous`) ' +
      'and writes nothing. With `type_id`, `apply` writes the fields a refresh showed over the type (only after the ' +
      'user confirms them), and `k_values: true` the K values it showed, sent back as `shown_k_values`. 409 when the ' +
      'profile no longer gives an applied value, or Bambuddy now gives other K values. Reads Bambuddy (GET only), writes ScadBuddy only.',
    input: z.object({
      type_id: z.string().uuid().optional().describe("A type's `id`; omit for every type"),
      refresh: z.boolean().default(false).describe('Answer the differences and write nothing'),
      apply: z
        .partialRecord(
          z.enum(['nozzle_temp_min', 'nozzle_temp_max', 'bed_temp_min', 'bed_temp_max', 'density_g_cm3', 'diameter_mm', 'drying_temp_c', 'drying_hours']),
          z.union([z.number(), z.string()]),
        )
        .optional()
        .describe('The confirmed fields and values a refresh showed, to write over the type'),
      k_values: z.boolean().optional().describe('With `apply`: also write the K values the refresh showed'),
      shown_k_values: z
        .array(
          z.object({
            printer_id: z.string().uuid(),
            extruder: z.number().int().min(0),
            nozzle_diameter: z.string(),
            k_value: z.string().describe('A decimal, e.g. `0.02`'),
            nozzle_type: z.string().nullable().optional(),
            name: z.string().nullable().optional(),
            cali_idx: z.number().int().nullable().optional(),
            setting_id: z.string().nullable().optional(),
          }),
        )
        .optional()
        .describe("With `k_values: true`: the refresh's `k_values` (`type_id` and `source` are dropped: the server sets the source); `[]` when it showed none"),
    }).refine((o) => o.k_values !== true || o.shown_k_values !== undefined, {
      message: "k_values needs shown_k_values: the refresh's K values ([] for none)",
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/types/fill-from-profile', 'POST /api/v1/filament/types/{type_id}/fill-from-profile'],
    summarize: ({ type_id, refresh, apply }) =>
      `${refresh ? 'Compare' : apply ? 'Apply profile values to' : 'Fill'} ${type_id ? `spool type ${type_id}` : 'every spool type'} from the slicer profile`,
    handler: async ({ type_id, refresh, apply, k_values, shown_k_values }, { backend }) => {
      if (type_id === undefined) {
        if (apply !== undefined || k_values !== undefined || shown_k_values !== undefined) throw new ToolError('apply and k_values need a type_id')
        return json(
          await ok(backend.POST('/api/v1/filament/types/fill-from-profile', { body: { refresh } }), 'fill the spool types from their profiles'),
        )
      }
      return json(
        await ok(
          backend.POST('/api/v1/filament/types/{type_id}/fill-from-profile', {
            params: { path: { type_id } },
            body: {
              refresh,
              ...(apply !== undefined ? { apply } : {}),
              ...(k_values !== undefined ? { k_values } : {}),
              ...(shown_k_values !== undefined ? { shown_k_values } : {}),
            },
          }),
          `fill spool type ${type_id} from its profile`,
        ),
      )
    },
  }),

  defineTool({
    name: 'filament_spool_list',
    label: 'List filament spools',
    description:
      "ScadBuddy's spools (one physical reel each) by number, each with its `effective` values (its own " +
      "override, else its type's), the fields it `overridden`, the `names` behind its ids, `low_stock`, and " +
      '`grams_left` from its ledger. ' +
      '`needs_type: true` lists only spools with no type yet, `false` only typed ones. Archived spools only ' +
      'with `include_archived`. `q` keeps the spools whose number, brand, material, subtype, colour name, hex, ' +
      "place, tray, type code, type's supplier SKU or open tag's UID or tray UUID (from 4 hex digits; `:` and `-` " +
      'are left out, so `04:A1:B2` finds tag `04A1B2…`) match it.',
    input: z.object({
      include_archived: z.boolean().default(false),
      needs_type: z.boolean().optional(),
      q: searchQuery,
    }),
    risk: 'read',
    source: SOURCE,
    routes: [
      'GET /api/v1/filament/spools',
      'GET /api/v1/filament/types',
      'GET /api/v1/filament/inventory',
      'GET /api/v1/filament/tags',
      ...SEARCH_ROUTES,
    ],
    handler: async ({ include_archived, needs_type, q }, { backend }) => {
      const spools = await ok(
        backend.GET('/api/v1/filament/spools', { params: { query: { include_archived, needs_type } } }),
        'list the spools',
      )
      const tokens = parseSearch(q)
      if (!tokens.length) return json(spools)
      // The type codes, SKUs, trays and tags only add to the search: one that cannot be read leaves its field out.
      const [types, skus, trays, tags] = await Promise.all([
        orElse(
          ok(backend.GET('/api/v1/filament/types', { params: { query: { include_archived: true } } }), 'list the spool types'),
          [],
        ),
        orElse(typeSkus(backend), new Map<string, string[]>()),
        orElse(spoolTrays(backend), new Map<string, string>()),
        orElse(spoolTags(backend), new Map<string, string[]>()),
      ])
      const codes = new Map(types.map((type) => [type.id, type.code]))
      return json(
        spools.filter((spool) =>
          matchesSearch(
            spoolSearchDoc(spool, {
              typeCode: spool.type_id ? codes.get(spool.type_id) : null,
              skus: spool.type_id ? skus.get(spool.type_id) : undefined,
              tray: trays.get(spool.id),
              tags: tags.get(spool.id),
            }),
            tokens,
          ),
        ),
      )
    },
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
      '`unit_cost`; `null` if none), `type_suppliers` (the unarchived products of its type from unarchived suppliers, each with its ' +
      'newest price and price per kg), and `drying_defaults`: the `temp_c` and `hours` a drying records when it leaves them out ' +
      "(filament_spool_add_event), each from the spool's own `drying_temp_c`/`drying_hours`, else its type's, else its " +
      "subtype's, else its material's, else the Settings default (`filament_drying_temp_c`, `filament_drying_hours`), " +
      'with `temp_from` and `hours_from` naming the level (`spool`, `type`, `subtype`, `material`, `setting`; `null` when ' +
      'none sets it).',
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
      "clears it so the spool takes its type's value again. `drying_temp_c` (whole °C) and `drying_hours` (a decimal " +
      "string) override its type's drying values, which a drying that leaves them out records. A new spool needs a `type_id` (filament_type_list) " +
      'or, while it needs a type, a `material_id`; `grams_left` is its first reading. Each edit is one `edited` ' +
      'ledger event. To record a weighing use filament_spool_add_event; to archive use filament_spool_archive. ' +
      'When adding, `quantity` (1 to 100) makes that many identical spools in one request, each with its own number, ' +
      "and `copy_of` (a spool's `id`) starts from that spool: its type, own overrides, note, own presets and K " +
      'values and its place unless that is a tray, never its number, tag, tray, weight or weight lock; `fields` ' +
      'then change the copy. Either answers the list of spools made, and a resend of the same call makes none twice; ' +
      'a resend of a copy reads the source again, and when the source changed in between it makes a new set from it. ' +
      '422 when an effective minimum temperature is above its maximum or the spool would have neither type nor ' +
      'material. Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      spool_id: z.string().uuid().optional().describe("A spool's `id`; omit to add"),
      fields: z.record(z.string(), z.unknown()).default({}).describe('The spool fields to set'),
      quantity: z.number().int().min(1).max(100).optional().describe('When adding: how many identical spools to make'),
      copy_of: z.string().uuid().optional().describe("When adding: the `id` of a spool to copy"),
    }),
    risk: 'write',
    source: SOURCE,
    routes: [
      'POST /api/v1/filament/spools',
      'PATCH /api/v1/filament/spools/{spool_id}',
      'POST /api/v1/filament/spools/bulk',
      'GET /api/v1/filament/spools/{spool_id}/copy',
    ],
    summarize: ({ spool_id, quantity, copy_of }) =>
      spool_id
        ? `Edit spool ${spool_id}`
        : `Add ${quantity && quantity > 1 ? `${quantity} spools` : 'a spool'}${copy_of ? ` copied from ${copy_of}` : ''}`,
    handler: async ({ spool_id, fields, quantity, copy_of }, ctx) => {
      const { backend } = ctx
      if (spool_id !== undefined && (quantity !== undefined || copy_of !== undefined)) {
        throw new ToolError('`quantity` and `copy_of` are for adding a spool; leave out `spool_id`')
      }
      if (spool_id === undefined && quantity === undefined && copy_of === undefined) {
        return json(await ok(backend.POST('/api/v1/filament/spools', { body: fields as SpoolCreate }), 'add a spool'))
      }
      if (spool_id === undefined) {
        const draft: SpoolDraft = copy_of
          ? await ok(
              backend.GET('/api/v1/filament/spools/{spool_id}/copy', { params: { path: { spool_id: copy_of } } }),
              `copy spool ${copy_of}`,
            )
          : { spool: {} as SpoolDraft['spool'] }
        // One key per call: a resend after a lost answer makes no second set of spools. A
        // resend reads the copy again, so its key names that draft too (R-7150-8): a changed
        // source is a new request, never a 422 for a key sent with another body.
        const body = { ...draft, spool: { ...draft.spool, ...fields }, quantity: quantity ?? 1 } as SpoolBulkCreate
        const key = copy_of
          ? createHash('sha256').update(`${idempotencyKey(ctx)}\x1f${JSON.stringify(draft)}`).digest('hex')
          : idempotencyKey(ctx)
        return json(
          await ok(
            backend.POST('/api/v1/filament/spools/bulk', {
              body,
              headers: { 'Idempotency-Key': key },
            }),
            'add the spools',
          ),
        )
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
          slicer_filament: z.string().describe("The slicer's filament id, e.g. `GFA00`, or a ScadBuddy preset as `sb-<id>`"),
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
      'ledger holds a print, Bambuddy links it, or it is being created in Bambuddy (an open create, failed or ' +
      'unanswered ones included; the next pull would make it again): use filament_spool_archive instead. Only when the user asks. Writes ScadBuddy only.',
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
    name: 'filament_spool_history',
    label: "Read a spool's history and metrics",
    description:
      "A spool's timeline and metrics: the newest 50 ledger `events` (newest first, with `actor`, `order_id` " +
      'and `archive_id`), its `metrics` (use, waste, cost used, value left, burn rate, empty day, days since ' +
      'opened and last use, hours outside dry storage, AMS error), `type_ams_error`, `currency` and ' +
      '`window_days`. `truncated` is true when older events exist. filament_spool_events lists every event, oldest first, so the ' +
      'older events are at the start of its list. ' +
      'The grams-left `series` is left out. 404 for no such spool.',
    input: z.object({ spool_id: z.string().uuid().describe("A spool's `id` from filament_spool_list") }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/spools/{spool_id}/history'],
    handler: async ({ spool_id }, { backend }) => {
      const { series: _series, events, ...rest } = await ok(
        backend.GET('/api/v1/filament/spools/{spool_id}/history', { params: { path: { spool_id } } }),
        `read the history of spool ${spool_id}`,
      )
      return json({ ...rest, events: events.slice(0, HISTORY_EVENTS), truncated: events.length > HISTORY_EVENTS })
    },
  }),

  defineTool({
    name: 'filament_forecast',
    label: 'Forecast when spools and types run out',
    description:
      "Each unarchived spool's burn rate (grams per day over the last `window_days`) and the day it runs " +
      "out, and each spool type's stock, burn, days of stock, empty day and reorder day (its lead time and " +
      'safety margin taken off the empty day). `reorder_now` is true when the reorder day is today or past. ' +
      'A spool or type with no use in the window has no burn, empty day or reorder day.',
    input: z.object({}),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/forecast'],
    handler: async (_input, { backend }) =>
      json(await ok(backend.GET('/api/v1/filament/forecast'), 'read the filament forecast')),
  }),

  defineTool({
    name: 'filament_metrics',
    label: 'Read stock, consumption and waste',
    description:
      "The unarchived spools' stock (spools, grams left, value) by `dimension`, the grams used and wasted " +
      'per `bucket` and dimension, and the waste by material, type and printer. `since` and `until` are ' +
      'dates (`YYYY-MM-DD`), both included; default the 180 days before today. 422 when `since` is after ' +
      '`until` or the range is over 1096 days.',
    input: z.object({
      dimension: z.enum(['material', 'brand', 'type', 'supplier', 'location']).optional().describe('Default material'),
      bucket: z.enum(['week', 'month']).optional().describe('Default month'),
      since: DATE.optional(),
      until: DATE.optional(),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/metrics'],
    handler: async ({ dimension, bucket, since, until }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/metrics', { params: { query: { dimension, bucket, since, until } } }),
          'read the filament metrics',
        ),
      ),
  }),

  defineTool({
    name: 'filament_purchasing_metrics',
    label: 'Read price per kg and supplier spend',
    description:
      "Each spool type's price per kg at each supplier over time (one series per currency), and the " +
      "confirmed invoices' totals per supplier, month and currency, never converted. `since` and `until` " +
      'are dates (`YYYY-MM-DD`), both included; default the 365 days before today. 422 when `since` is after ' +
      '`until` or the range is over 1096 days.',
    input: z.object({ since: DATE.optional(), until: DATE.optional() }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/metrics/purchasing'],
    handler: async ({ since, until }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/metrics/purchasing', { params: { query: { since, until } } }),
          'read the purchasing metrics',
        ),
      ),
  }),

  defineTool({
    name: 'filament_material_number_stats',
    label: 'Read stock and use by material number',
    description:
      "Per material number (a spool type's `code`, which Bambuddy calls `material_number`, case folded; " +
      '`key` null for untyped spools): every type of that code with `archived`, the unarchived spools, their ' +
      "grams left and value at cost (in `currency`, the shop's), and `used_g` and `wasted_g`, the plates' " +
      'grams from `since` to `until` counted as `filament_metrics` counts consumption. `since` and `until` ' +
      'are dates (`YYYY-MM-DD`), both included; default the 180 days before today. 422 when `since` is ' +
      'after `until` or the range is over 1096 days.',
    input: z.object({ since: DATE.optional(), until: DATE.optional() }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/metrics/material-numbers'],
    handler: async ({ since, until }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/metrics/material-numbers', { params: { query: { since, until } } }),
          'read the material number stats',
        ),
      ),
  }),

  defineTool({
    name: 'filament_cost_per_kg',
    label: 'Work out a cost per kg',
    description:
      'What `price` (paid for `quantity` spools, default 1), `tax` and `shipping` (default 0) come to per kg ' +
      'when each spool holds `net_weight_g` grams of filament: `cost_per_kg` with all three in (what an ' +
      'invoice line gives its spools) and `price_per_kg` with shipping out (a supplier price point). Money is ' +
      "a decimal string of at most four places; the answer is exact, rounded half up to four, in the shop's " +
      '`currency`. It writes nothing: to keep the result, set the type\'s `cost_per_kg`.',
    input: z.object({
      price: MONEY,
      tax: MONEY.optional(),
      shipping: MONEY.optional(),
      quantity: z.number().int().min(1).max(1000).optional().describe('Spools the price paid for; default 1'),
      net_weight_g: z.number().int().min(1).max(100_000).describe('Grams of filament in one spool, without the spool'),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/cost-per-kg'],
    handler: async ({ price, tax, shipping, quantity, net_weight_g }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/cost-per-kg', { params: { query: { price, tax, shipping, quantity, net_weight_g } } }),
          'work out the cost per kg',
        ),
      ),
  }),

  defineTool({
    name: 'filament_rfid_status',
    label: 'Read the RFID override mode',
    description:
      "Whether the RFID override mode is on, whether the setting `filament_auto_add_unknown_rfid` is on (off holds " +
      "Bambuddy's auto-add off too), and where Bambuddy's auto-add of unknown RFID spools stands: " +
      '`applied` (the mode or the setting holds it, and Bambuddy\'s value is false), `pending` (a write has not landed or Bambuddy is down; ' +
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
      '`locations`; the sync writes Bambuddy only for a type set to `sb_to_bb` or `two_way`, never for `tags` (the ' +
      "spool refresh's tag drain sends those). `instance` is the Bambuddy the links belong to; `instance_changed` " +
      'is true when that is not the configured Bambuddy, and the sync is then stopped until ' +
      'filament_sync_reset_links resets the links. `last_pass_at` is when the ' +
      "last pass ended, `last_pass_started_at` when it started, `last_error` its error (null when it worked), `last_ok_at` the last pass with no error (a skipped " +
      "one too), `counts` the last pass's (`written`, `failed`, `deferred`, `conflicts`, `unmapped_k`). `errors` " +
      'lists each item whose last write to Bambuddy failed, or was held back (`name_taken`: another Bambuddy row ' +
      "has its name, so it was never sent), with `kind`, its ScadBuddy `sb_id`, its `label` (a spool's `#N`, a " +
      "spool type's code, any other row's name; null when the item is gone), `error`, `attempts` and `at`, " +
      'newest first: this is why a spool is not in Bambuddy. `skipped` says why no pass runs now: ' +
      '`no_bambuddy` or `nothing_pushes` (no data type but `tags` is set to push); else null. Reads ScadBuddy only.',
    input: z.object({}),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/sync/status'],
    handler: async (_input, { backend }) =>
      json(await ok(backend.GET('/api/v1/filament/sync/status'), 'read the Bambuddy sync status')),
  }),

  defineTool({
    name: 'filament_sync_passes',
    label: 'Read the Bambuddy sync history',
    description:
      "The ScadBuddy-to-Bambuddy filament sync's passes, newest first, failed ones too (filament_sync_status " +
      'holds only the last). Each has its `id`, the Bambuddy `instance`, its `trigger` (`timer`, `change`: a ' +
      'write\'s nudge, `full`: a full pass asked for such as Sync now, `more`: the pass before it left writes; ' +
      'null when unknown), `started_at`, `ended_at`, the counts `written`, `failed`, `deferred`, `conflicts` and ' +
      '`unmapped_k`, `skipped` (`no_bambuddy` or `nothing_pushes`), `instance_changed` and `error` (a label, null ' +
      "when it worked; `crud_unavailable:<failure>` or `crud_refused:<status>` when ScadBuddy's own API failed " +
      'during the pass, `internal` for any other failure outside Bambuddy). A page holds up to `limit` passes (at most 200); when `next_cursor` is set, pass it back ' +
      'as `cursor` for the next page. Passes older than the setting `filament_sync_pass_ttl_days` (30 by default) ' +
      'are gone. Reads ScadBuddy only.',
    input: z.object({
      cursor: z.string().optional(),
      limit: z.number().int().min(1).max(200).default(20),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/sync/passes'],
    handler: async (query, { backend }) =>
      json(
        await ok(backend.GET('/api/v1/filament/sync/passes', { params: { query } }), 'read the Bambuddy sync history'),
      ),
  }),

  defineTool({
    name: 'filament_sync_run',
    label: 'Sync with Bambuddy now',
    description:
      'Ask for a full ScadBuddy-to-Bambuddy sync pass now. It runs in the background on the print worker: it reads ' +
      'Bambuddy, then writes it only for the data types `filament_sync` sets to push (`sb_to_bb` or `two_way`), ' +
      "never `tags` (the spool refresh's tag drain sends those); " +
      'see filament_sync_status for the directions. Answers `{started: true}` once the pass is asked for; read ' +
      'filament_sync_status afterwards for how it went. 409 `no_bambuddy` with no Bambuddy configured, ' +
      '`nothing_pushes` when no data type but `tags` is set to push. 503 when the pass could not be asked for (the settings ' +
      'read or the workflow service failed, or took over 5 s; `detail` ends in an error label, `internal` for a ' +
      'failure outside Bambuddy): nothing started, so try again later. Writes Bambuddy to ' +
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
    name: 'filament_sync_reset_links',
    label: 'Reset the Bambuddy links',
    description:
      'After the Bambuddy URL changed, the links (Bambuddy ids) belong to the old Bambuddy and the sync is stopped: ' +
      'filament_sync_status says `instance_changed: true`, and the pull is stopped too. Call it first with ' +
      "`expect_kept: true`: it reads the configured Bambuddy's spools, and when that Bambuddy holds every linked " +
      'spool as ScadBuddy last saw it (the same Bambuddy at a new address), nothing is deleted: every link now ' +
      'belongs to it, and the answer has `kept: true` and zero counts. When it cannot prove that (Bambuddy could ' +
      'not be read, or it is another Bambuddy, or every linked spool ScadBuddy could compare (a time on both sides) was edited in Bambuddy since the last pull so ' +
      'none matches what ScadBuddy saw and the proof has nothing to stand on), it answers 409 `proof_changed` and ' +
      'deletes nothing; tell the user which case it is if you know (the dry run `GET /filament/sync/reset-links` ' +
      'has `changed_since_pull`). Only then, ' +
      'and only after the user agreed to the delete below, call it again with `expect_kept: false` (the default), ' +
      'which reads no Bambuddy and deletes, in one transaction, every spool link, the ' +
      'supplier, location, insert type and spool type links (their Bambuddy ids, hashes and seen copies), the open ' +
      'create intents, the error rows, the Bambuddy spools the pull ignores and the recorded instance; then the full ' +
      'pass links the configured Bambuddy afresh. No ScadBuddy spool, type, supplier or location ' +
      'is deleted. After a delete (`kept: false`) a spool is linked again only to the Bambuddy spool that holds its RFID tag; other ' +
      'spools are created on the side the sync sends them to, so tell the user an untagged spool can end up twice. ' +
      'Either way it asks for a full pass. ' +
      '`confirm_instance` must be the instance the links belong to: filament_sync_status `instance`. Answers the ' +
      'instance, `kept` and the counts deleted (`deleted`: `spools`, `suppliers`, `locations`, `insert_types`, ' +
      '`filaments`, `intents`, `errors`, `ignored`). 409 `confirm_mismatch` when `confirm_instance` is not the ' +
      "links' instance, `same_instance` when they belong to the configured Bambuddy, `nothing_to_reset` when no " +
      'instance is recorded, `proof_changed` as above; each deletes nothing. Approved first.',
    input: z.object({
      confirm_instance: z
        .string()
        .min(1)
        .max(64)
        .describe('The instance the links belong to, as filament_sync_status `instance` gives it'),
      expect_kept: z
        .boolean()
        .default(false)
        .describe(
          'true: keep the links only while the configured Bambuddy is proven the same one, else 409 ' +
            '`proof_changed` with nothing deleted; false: delete the links without reading Bambuddy',
        ),
    }),
    risk: 'outward',
    source: SOURCE,
    routes: ['POST /api/v1/filament/sync/reset-links'],
    summarize: ({ confirm_instance, expect_kept }) =>
      expect_kept
        ? `Keep the Bambuddy links of instance ${confirm_instance} if the configured Bambuddy is the same one`
        : `Delete the Bambuddy links of instance ${confirm_instance}`,
    handler: async ({ confirm_instance, expect_kept }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/sync/reset-links', { body: { confirm_instance, expect_kept } }),
          'reset the Bambuddy links',
        ),
      ),
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
    label: 'Record a weighing, drying or counter reset',
    description:
      'Record on a spool a weighing (`kind: weighed`, `grams` = the filament left, without the empty spool; ' +
      '`grams_left` starts again from it) or a drying (`kind: dried`, `temp_c`, `hours`). A drying that leaves ' +
      "out `temp_c` or `hours` records the spool's `drying_defaults` (filament_spool_get: its own drying values, else its " +
      "type's, else its subtype's, else its material's, else the Settings default); `null` records none. " +
      "`kind: consumed_reset` (no grams, temp_c or hours) starts the spool's used counter (`consumed_g`) again from 0 g; the " +
      'grams left stay. Only what the user says. Writes ScadBuddy only; a reset reaches Bambuddy at the next sync when weights ' +
      'sync to it.',
    input: z.object({
      spool_id: z.string().uuid(),
      kind: z.enum(['weighed', 'dried', 'consumed_reset']),
      source: z.enum(['person', 'scale']).default('person'),
      grams: z.number().min(0).optional(),
      temp_c: z.number().int().min(0).nullable().optional().describe("Left out: the spool's drying default"),
      hours: z.string().nullable().optional().describe("A decimal, e.g. `8`; left out: the spool's drying default"),
      note: z.string().optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/{spool_id}/events'],
    summarize: ({ spool_id, kind, grams }) =>
      kind === 'weighed'
        ? `Record ${grams ?? '?'} g on spool ${spool_id}`
        : kind === 'consumed_reset'
          ? `Reset the used counter of spool ${spool_id}`
          : `Record a drying of spool ${spool_id}`,
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
    name: 'filament_spools_reset_consumed',
    label: 'Reset the used counter of spools',
    description:
      'Start the used counter (`consumed_g`) of 1 to 500 spools again from 0 g, as Bambuddy\'s "reset consumed" does (#7149). ' +
      'The grams left, the lifetime use and the cost stay. Answers `reset` (how many) and `not_found` (ids with no spool). ' +
      'Writes ScadBuddy; a linked spool has its Bambuddy counter reset at the next sync when weights sync to Bambuddy.',
    input: z.object({ spool_ids: z.array(z.string().uuid()).min(1).max(500) }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/reset-consumed'],
    summarize: ({ spool_ids }) => `Reset the used counter of ${spool_ids.length} ${spool_ids.length === 1 ? 'spool' : 'spools'}`,
    handler: async (body, { backend }) =>
      json(await ok(backend.POST('/api/v1/filament/spools/reset-consumed', { body }), 'reset the used counters')),
  }),

  defineTool({
    name: 'filament_spools_bulk_update',
    label: 'Edit several spools at once',
    description:
      'Set the same `patch` on 1 to 500 spools in one request (#7151): any field filament_spool_upsert edits but ' +
      '`archived` (use filament_spools_bulk_archive). `null` on an override clears it. All or none: when one spool ' +
      'refuses the patch, nothing changes and the error names that spool by its number. Each changed spool gets its ' +
      'own `edited` ledger event; a resend changes nothing more. Answers `updated` and `not_found` (ids with no spool). ' +
      'Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      spool_ids: z.array(z.string().uuid()).min(1).max(500),
      patch: z.record(z.string(), z.unknown()).describe('The spool fields to set on each spool'),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/bulk-update'],
    summarize: ({ spool_ids, patch }) =>
      `Set ${Object.keys(patch).join(', ') || 'nothing'} on ${spool_ids.length} ${spool_ids.length === 1 ? 'spool' : 'spools'}`,
    handler: async ({ spool_ids, patch }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/spools/bulk-update', { body: { spool_ids, patch } as SpoolBulkUpdate }),
          'edit the spools',
        ),
      ),
  }),

  defineTool({
    name: 'filament_spools_bulk_archive',
    label: 'Archive or restore several spools',
    description:
      '`archived: true` takes 1 to 500 spools off the lists in one request; `false` puts them back (#7151). Each spool ' +
      'changed gets its own ledger event, and an archived one loses its open tag binding, as filament_spool_archive does. ' +
      'Answers `changed`, `unchanged` (already so) and `not_found` (ids with no spool). Only what the user says. Writes ' +
      "ScadBuddy; with tag sync on (filament_sync.tags), an archived spool's tag is then cleared in Bambuddy.",
    input: z.object({
      spool_ids: z.array(z.string().uuid()).min(1).max(500),
      archived: z.boolean().default(true),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/spools/bulk-archive', 'POST /api/v1/filament/spools/bulk-restore'],
    summarize: ({ spool_ids, archived }) =>
      `${archived ? 'Archive' : 'Restore'} ${spool_ids.length} ${spool_ids.length === 1 ? 'spool' : 'spools'}`,
    handler: async ({ spool_ids, archived }, { backend }) =>
      json(
        await ok(
          archived
            ? backend.POST('/api/v1/filament/spools/bulk-archive', { body: { spool_ids } })
            : backend.POST('/api/v1/filament/spools/bulk-restore', { body: { spool_ids } }),
          `${archived ? 'archive' : 'restore'} the spools`,
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
    name: 'filament_code_resolve',
    label: 'Resolve a label code',
    description:
      'What a typed label code names: `L-12` is a place, `S-142` a spool. Any case, with or without the dash. ' +
      'Returns the kind, the id, the name and `archived`; an archived thing still resolves. 404 `code_unknown` ' +
      'for a code that names nothing.',
    input: z.object({ code: z.string().min(1).describe('A label code, such as `L-12` or `s142`') }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/codes/{code}'],
    handler: async ({ code }, { backend }) =>
      json(await ok(backend.GET('/api/v1/codes/{code}', { params: { path: { code } } }), `resolve code ${code}`)),
  }),

  defineTool({
    name: 'filament_location_get',
    label: 'Get a filament location',
    description:
      'One place with its code, its path, its children and its spools. 404 for no such place. ' +
      '`location_id` is a place `id` from filament_location_list.',
    input: z.object({ location_id: z.string().uuid() }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/locations/{location_id}'],
    handler: async ({ location_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/locations/{location_id}', { params: { path: { location_id } } }),
          `get location ${location_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_printer_labels',
    label: 'List printer label codes',
    description:
      "The label of each printer's own place, each AMS unit with its slots, and the external holder, each with its " +
      '`code`, `name` and `depth` (0 printer, 1 unit or external holder, 2 slot), and the `base_url` a QR opens. ' +
      '`printer_ids` limits it; omit for every printer. It may create the printer\'s own place and the place of each ' +
      'AMS unit, slot and external holder the printer\'s latest report lists. That is derived data: capture makes ' +
      'the same places (the printer\'s own once it has an AMS unit or holder place, a slot\'s when a spool loads there), so this ' +
      'tool is `read` tier. `no_report` names the printers with no report that lists a unit or a holder: their ' +
      'empty slots get labels only once a spool has been loaded. 409 `no_printer_kind` when no location kind is ' +
      'marked as a printer.',
    input: z.object({ printer_ids: z.array(z.string().uuid()).optional() }),
    risk: 'read',
    source: SOURCE,
    routes: ['POST /api/v1/filament/printer-labels'],
    handler: async ({ printer_ids }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/printer-labels', { body: { printer_ids: printer_ids ?? null } }),
          'list the printer labels',
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
      'subtype, brand, category and effect names resolve by key against every name and alias (`dual-color` is ' +
      '`Dual Color`) and are added to their catalog when missing; `insert_type`, `type` ' +
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
      "Pull Bambuddy's spools, archived ones too, into ScadBuddy's own inventory once (the spool refresh Schedule also " +
      'does this). A linked spool takes what Bambuddy changed since the last pull, unless ScadBuddy changed that ' +
      'field later. A Bambuddy spool whose tag or record a ScadBuddy spool already holds is linked. Any other ' +
      'becomes a new spool, typed by `material_number`, else by brand, material, subtype, colour and label weight, ' +
      'else left needing a type. An archived ScadBuddy spool is never restored or changed (listed as `archived`). ' +
      'A Bambuddy spool Bambuddy archived before any pull linked it becomes an archived spool with its usage, counted ' +
      'in `created` and `archived_created`. When the usage rows cannot be read, such a spool is not made yet ' +
      '(`archived_deferred`, with its Bambuddy id); a later pull makes it. ' +
      'Returns the counts and ids created, linked, updated, still needing a type and archived, and the Bambuddy ' +
      'rows it could not read (`invalid`) or pull (`failed_bb_ids`). Also: `rfid_duplicates` (Bambuddy spools made ' +
      'for a tag a linked spool holds, left alone), `catalog_failed` and `catalog_skipped` (Bambuddy\'s spool ' +
      'catalog), `names_skipped` (catalog names with no letter or digit, left out), and `usage_booked` and ' +
      '`usage_unlinked` (Bambuddy usage rows booked, and the rows of an unlinked spool later than the last ' +
      "read an earlier pull booked whole; a failed body's rows are counted again). " +
      '409 when no Bambuddy is configured, or when the Bambuddy URL changed during the import (import again). ' +
      '503 when the print worker did not finish in time: it may still be running, so check the spools before ' +
      'importing again. 502 when ScadBuddy refused what the worker read (importing again gets the same answer). ' +
      "ScadBuddy's print worker reads Bambuddy; this writes ScadBuddy only.",
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
      '`no_remain`, `stale`), the `spools` booked, and `stale_printers`: each printer whose spools are in `stale`, ' +
      'with its `id` and `name` (`null` when the printer is gone). Name those printers to the user. Writes ScadBuddy only.',
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
      'its last value and time, `reachable`, `alerting` (the value is outside its alert band) and `last_notification` ' +
      '(the newest notification it queued when it started or stopped alerting with `notify` on: `kind`, `state`, ' +
      '`channel`, `sent_at`, and `error` when Bambuddy did not deliver it; null for none).',
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
      'the printer report. With `notify` on, ScadBuddy sends one notification through Bambuddy\'s notification ' +
      'channels when the sensor starts alerting and one when it stops (after 10 minutes back inside its band), ' +
      'within about a minute; turning `notify` off stops what is not sent yet. Only what the user says. Writes ' +
      'ScadBuddy only.',
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

const catalogRow = (args: Record<string, unknown>): LinkKind => `catalog:${String(args.kind)}`

/** #6869: each answer's entities carry their `ui_path` (`filamentLinks.ts`); these tools answer one kind of entity, or a list of it. */
export const filamentTools: Tool[] = linkingTools(tools, {
  filament_catalog_list: catalogRow,
  filament_catalog_upsert: catalogRow,
  filament_catalog_resolve: catalogRow,
  filament_catalog_set_aliases: catalogRow,
  filament_type_list: 'type',
  filament_type_get: 'type',
  filament_type_upsert: 'type',
  filament_spool_list: 'spool',
  filament_spool_get: 'spool',
  filament_spool_upsert: 'spool',
  filament_spool_archive: 'spool',
  filament_spool_move: 'spool',
  filament_location_list: 'location',
  filament_location_get: 'location',
  filament_location_upsert: 'location',
})
