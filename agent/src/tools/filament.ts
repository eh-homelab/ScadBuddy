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
]
