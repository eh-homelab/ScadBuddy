import { z } from 'zod'
import { ok } from './call.js'
import { defineTool, json, ToolError, type Tool } from './registry.js'

// ScadBuddy's own filament inventory (#6261, epic #6260): the catalogs a spool type or a
// spool picks from. Write tier: nothing leaves ScadBuddy.
// Routes: backend/scadbuddy/api/filament.py.

const SOURCE = "ScadBuddy's own filament inventory"

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
            backend.POST('/api/v1/filament/catalog/{kind}', { params: { path: { kind } }, body: fields as never }),
            `add to the ${kind} catalog`,
          ),
        )
      }
      return json(
        await ok(
          backend.PATCH('/api/v1/filament/catalog/{kind}/{row_id}', {
            params: { path: { kind, row_id } },
            body: (archived === undefined ? fields : { ...fields, archived }) as never,
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
      'book row of its material, subtype, brand and colour in step; types that differ only in alpha share one ' +
      'row. `archived: true` takes a type off the lists and frees its code. 409 when an unarchived type has the ' +
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
            body: (archived === undefined ? fields : { ...fields, archived }) as never,
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
            body: k_values as never,
          }),
          `set the K values of spool type ${type_id}`,
        ),
      ),
  }),
]
