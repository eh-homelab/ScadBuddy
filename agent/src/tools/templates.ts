import { z } from 'zod'
import { ok } from './call.js'
import { slug } from './common.js'
import { defineTool, json, ToolError, type Tool } from './registry.js'

// Starting a model (issue #252: "Start from one of the bundled models/
// examples, from a blank template with the customizer annotations and color()
// conventions already in place, or by duplicating an existing model"). A
// bundled example or an existing model is a duplicate, kept linked to it as
// its upstream (POST /models/{slug}/duplicate); the blank template is a create
// from BLANK_TEMPLATE (POST /models). duplicate_model and create_model stay for
// callers that want either step on its own.

/**
 * The blank template: the plugin's `authoring` skill's conventions in the
 * smallest model that shows them (plugins/scadbuddy/skills/authoring/SKILL.md
 * sections 2, 3, 5 and 6): the header comment naming the extruders, customizer
 * groups with captions, sliders and a text limit, a `// font` field on a family
 * the image installs, one `// color` parameter per extruder in extruder order,
 * every solid in a `color()`, and `$fn` under [Hidden].
 *
 * Measured 2026-09-29 on this exact string in the Dockerfile's `base` stage
 * (OpenSCAD 2026.09.28 plus the image's fonts), and re-measured for the #752
 * review: `--backend=Manifold -o out.3mf` renders one manifold object whose
 * triangles are all on "Color 1" (266) and "Color 2" (738), none on "Default";
 * `-o model.param` exports the eight parameters in groups Size, Label and
 * Colours. The counts hold only for that build and DejaVu Sans Bold: after an
 * image bump, write BLANK_TEMPLATE to model.scad and run both commands in
 * `docker build --target base` again.
 */
export const BLANK_TEMPLATE = [
  '// A new ScadBuddy template: a plate with a raised label. Replace this line',
  '// with what the model makes.',
  '//',
  '// Written to the MakerWorld Parametric Model Maker customizer conventions so',
  '// the same file works unchanged on MakerWorld and in ScadBuddy.',
  '//',
  '// The colour parameters are the extruder order: base_color is extruder 1,',
  '// text_color is extruder 2.',
  '',
  '/* [Size] */',
  '',
  '// Plate length (X) in mm',
  'length = 60; // [20:1:200]',
  '',
  '// Plate width (Y) in mm',
  'width = 30; // [10:1:200]',
  '',
  '// Plate thickness in mm',
  'thickness = 3; // [1:0.2:10]',
  '',
  '/* [Label] */',
  '',
  '// Text on the plate',
  'label = "Hello"; // 20',
  '',
  '// Text height in mm',
  'text_size = 10; // [4:0.5:40]',
  '',
  '// Typeface',
  'font = "DejaVu Sans:style=Bold"; // font',
  '',
  '/* [Colours] */',
  '',
  '// Plate colour (extruder 1)',
  'base_color = "#2E86DE"; // color',
  '',
  '// Text colour (extruder 2)',
  'text_color = "#FFFFFF"; // color',
  '',
  '/* [Hidden] */',
  '',
  '$fn = 48;',
  'text_height = 1.2;',
  '',
  'color(base_color)',
  '    cube([length, width, thickness]);',
  '',
  'color(text_color)',
  '    translate([length / 2, width / 2, thickness])',
  '        linear_extrude(height = text_height)',
  '            text(label, size = text_size, font = font, halign = "center", valign = "center");',
  '',
].join('\n')

export const templateTools: Tool[] = [
  defineTool({
    name: 'create_from_template',
    description:
      'Start a new model under `name`: from `blank` (a small two-colour template with the customizer ' +
      'annotations, fonts and color() conventions already in place; replace its geometry), or from a ' +
      'bundled example or any existing model by slug (e.g. "builtin:storage-box"; list_models shows them), ' +
      'which is duplicated and kept linked to it as its upstream (for a model itself named "blank", use ' +
      'duplicate_model). Answers the new model; read its source ' +
      'with get_source and change it with apply_patch.',
    input: z.object({
      name: z.string().min(1).max(100),
      from: z.union([z.literal('blank'), slug]).describe('"blank", or the slug of a bundled example or model'),
      description: z.string().max(2000).optional().describe('For a blank model; a duplicate keeps its own'),
      tags: z.array(z.string()).optional().describe('For a blank model'),
    }),
    risk: 'write',
    routes: ['POST /api/v1/models', 'POST /api/v1/models/{slug}/duplicate'],
    handler: async ({ name, from, description, tags }, { backend }) => {
      if (from === 'blank') {
        return json(
          await ok(
            backend.POST('/api/v1/models', {
              body: { name, source: BLANK_TEMPLATE, description: description ?? '', tags: tags ?? [], force: false },
            }),
            `create ${name} from the blank template`,
          ),
        )
      }
      // A duplicate keeps its source's description and tags; refused rather than
      // dropped, so the caller knows (PR #752 review).
      if (description !== undefined || tags !== undefined) {
        throw new ToolError(
          `description and tags are for a blank model; a duplicate of ${from} keeps its own. ` +
            'Set them with update_model_details after creating it.',
        )
      }
      return json(
        await ok(
          backend.POST('/api/v1/models/{slug}/duplicate', { params: { path: { slug: from } }, body: { name } }),
          `create ${name} from ${from}`,
        ),
      )
    },
  }),
]
