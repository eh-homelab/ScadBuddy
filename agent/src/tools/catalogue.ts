import { z } from 'zod'
import { binary } from './binary.js'
import { command } from './command.js'
import { ok } from './call.js'
import { commit, decodeBase64, fileForm, slug } from './common.js'
import { defineTool, image, json, text, type Tool } from './registry.js'
import { page, PAGED, pageInput } from './pagination.js'

// Catalogue & models (issue #251): list, get, create, import, check, duplicate,
// delete, and edit details, README, source and thumbnail. Routes:
// backend/scadbuddy/api/models.py.

export const catalogueTools: Tool[] = [
  defineTool({
    name: 'list_models',
    description:
      'List the models in the catalogue (bundled templates and user models) with name, slug, tags, ' +
      'version, origin and upstream state.' + PAGED,
    input: z.object({ ...pageInput }),
    risk: 'read',
    source:
      'model metadata (names, descriptions, tags) written by model authors or imported from the web',
    routes: ['GET /api/v1/models'],
    handler: async (args, { backend }) =>
      json(page(await ok(backend.GET('/api/v1/models'), 'list models'), args, (m) => m.slug, 'list_models')),
  }),

  defineTool({
    name: 'get_model',
    description: "One model's metadata: name, description, tags, current version, libraries and upstream.",
    input: z.object({ slug }),
    risk: 'read',
    source:
      'model metadata (names, descriptions, tags) written by model authors or imported from the web',
    routes: ['GET /api/v1/models/{slug}'],
    handler: async ({ slug }, { backend }) =>
      json(await ok(backend.GET('/api/v1/models/{slug}', { params: { path: { slug } } }), `get model ${slug}`)),
  }),

  defineTool({
    name: 'get_source',
    description: "A model's current OpenSCAD source.",
    input: z.object({ slug }),
    risk: 'read',
    source:
      "OpenSCAD source (code and comments) written by the model's author, imported from the web or pulled from an upstream",
    routes: ['GET /api/v1/models/{slug}/source'],
    handler: async ({ slug }, { backend }) =>
      text(
        await ok(
          backend.GET('/api/v1/models/{slug}/source', { params: { path: { slug } }, parseAs: 'text' }),
          `get source of ${slug}`,
        ),
      ),
  }),

  defineTool({
    name: 'check_source',
    description:
      'Parse-check OpenSCAD source without saving it. Pass `slug` to resolve include/use against that ' +
      "model's directory, as a render would.",
    input: z.object({ source: z.string().max(1_000_000), slug: slug.optional() }),
    risk: 'read',
    source:
      "OpenSCAD's output for a model, including echo() text and other messages the model's source controls",
    routes: ['POST /api/v1/models/check'],
    handler: async ({ source, slug }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/models/check', { body: { source, slug: slug ?? null } }),
          'check source',
        ),
      ),
  }),

  defineTool({
    name: 'create_model',
    description:
      'Add a new model from OpenSCAD source. The slug is derived from the name. Refused when the source ' +
      'fails the parse check unless `force` is true.',
    input: z.object({
      name: z.string().min(1),
      source: z.string().max(1_000_000),
      description: z.string().default(''),
      tags: z.array(z.string()).default([]),
      force: z.boolean().default(false),
    }),
    risk: 'write',
    routes: ['POST /api/v1/models'],
    handler: async (body, ctx) =>
      json(await command(ctx, `create model ${body.name}`, (headers) => ctx.backend.POST('/api/v1/models', { body, headers }))),
  }),

  // `outward`, although the result is reversible, because the backend fetches a
  // URL the caller chose. SSRF into the cluster is already blocked by the backend
  // (backend/scadbuddy/library/url_import.py: https only, redirects included, each
  // hop vetted by `public_addresses()` so every resolved address must be public,
  // and the connection pinned to the vetted addresses). The tier is about
  // EXFILTRATION: a prompt-injected agent could encode data into a URL on a
  // public host it controls, so a human approves the fetch first (spec §8.2).
  defineTool({
    name: 'import_model',
    description:
      'Import a model from an https URL to its .scad source. The backend fetches the URL, so this needs a ' +
      'human approval.',
    input: z.object({
      url: z.string().url().max(2048),
      name: z.string().optional(),
      force: z.boolean().default(false),
    }),
    risk: 'outward',
    routes: ['POST /api/v1/models/import'],
    summarize: ({ url, name }) => `Fetch and import a model from ${url}${name ? ` as "${name}"` : ''}`,
    handler: async ({ url, name, force }, ctx) =>
      json(
        await command(ctx, `import ${url}`, (headers) =>
          ctx.backend.POST('/api/v1/models/import', { body: { url, name: name ?? null, force }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'duplicate_model',
    description: 'Duplicate a model (typically a bundled template) under a new name, keeping it linked as its upstream.',
    input: z.object({ slug, name: z.string().min(1) }),
    risk: 'write',
    routes: ['POST /api/v1/models/{slug}/duplicate'],
    handler: async ({ slug, name }, ctx) =>
      json(
        await command(ctx, `duplicate ${slug}`, (headers) =>
          ctx.backend.POST('/api/v1/models/{slug}/duplicate', { params: { path: { slug } }, body: { name }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'update_model_details',
    description: "Edit a model's name, description or tags. Omitted fields are unchanged.",
    input: z.object({
      slug,
      name: z.string().min(1).optional(),
      description: z.string().optional(),
      tags: z.array(z.string()).optional(),
    }),
    risk: 'write',
    routes: ['PATCH /api/v1/models/{slug}'],
    handler: async ({ slug, name, description, tags }, ctx) =>
      json(
        await command(ctx, `update ${slug}`, (headers) =>
          ctx.backend.PATCH('/api/v1/models/{slug}', {
            params: { path: { slug } },
            body: { name: name ?? null, description: description ?? null, tags: tags ?? null },
            headers,
          }),
        ),
      ),
  }),

  defineTool({
    name: 'update_source',
    description:
      "Replace a model's OpenSCAD source as one revision in its history (undo with restore_version). " +
      'Refused when the parse check fails unless `force` is true. Pass `base` (the `version` you read ' +
      'the source at) to have it refused if someone else saved since; for a small change, apply_patch.',
    input: z.object({
      slug,
      source: z.string().max(1_000_000),
      message: z.string().max(200).optional().describe('What the revision is called in the history'),
      force: z.boolean().default(false),
      base: commit.optional().describe('Refuse (409, naming the current revision) unless the model is still here'),
    }),
    risk: 'write',
    routes: ['PUT /api/v1/models/{slug}/source'],
    handler: async ({ slug, source, message, force, base }, ctx) =>
      json(
        await command(ctx, `update source of ${slug}`, (headers) =>
          ctx.backend.PUT('/api/v1/models/{slug}/source', {
            params: { path: { slug } },
            body: { source, message: message ?? null, force, base: base ?? null },
            headers,
          }),
        ),
      ),
  }),

  defineTool({
    name: 'delete_model',
    description: 'Delete a model and its outputs. Irreversible, so it needs a human approval.',
    input: z.object({ slug, force: z.boolean().default(false) }),
    risk: 'outward',
    routes: ['DELETE /api/v1/models/{slug}'],
    summarize: ({ slug }) => `Delete the model "${slug}" and its outputs`,
    handler: async ({ slug, force }, ctx) => {
      await command(ctx, `delete ${slug}`, (headers) =>
        ctx.backend.DELETE('/api/v1/models/{slug}', { params: { path: { slug }, query: { force } }, headers }),
      )
      return json({ deleted: slug })
    },
  }),

  defineTool({
    name: 'get_readme',
    description: "A model's README, as Markdown.",
    input: z.object({ slug }),
    risk: 'read',
    source:
      "the model's README, written by the model's author or imported with the model from the web",
    routes: ['GET /api/v1/models/{slug}/readme'],
    handler: async ({ slug }, { backend }) =>
      text(
        await ok(
          backend.GET('/api/v1/models/{slug}/readme', { params: { path: { slug } }, parseAs: 'text' }),
          `get README of ${slug}`,
        ),
      ),
  }),

  defineTool({
    name: 'set_readme',
    description: "Set a model's README (Markdown), as a revision in its history.",
    input: z.object({ slug, content: z.string().max(1_000_000) }),
    risk: 'write',
    routes: ['PUT /api/v1/models/{slug}/readme'],
    handler: async ({ slug, content }, ctx) =>
      json(
        await command(ctx, `set README of ${slug}`, (headers) =>
          ctx.backend.PUT('/api/v1/models/{slug}/readme', { params: { path: { slug } }, body: { content }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'delete_readme',
    description: "Remove a model's README, as a revision in its history.",
    input: z.object({ slug }),
    risk: 'write',
    routes: ['DELETE /api/v1/models/{slug}/readme'],
    handler: async ({ slug }, ctx) =>
      json(
        await command(ctx, `delete README of ${slug}`, (headers) =>
          ctx.backend.DELETE('/api/v1/models/{slug}/readme', { params: { path: { slug } }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'get_model_thumbnail',
    description: "A model's thumbnail image (its own, or its first output's plate image).",
    input: z.object({ slug }),
    risk: 'read',
    routes: ['GET /api/v1/models/{slug}/thumbnail'],
    handler: async ({ slug }, ctx) =>
      binary(
        ctx.backend.GET('/api/v1/models/{slug}/thumbnail', { params: { path: { slug } }, parseAs: 'stream' }),
        `get thumbnail of ${slug}`,
        ctx,
        { path: `/api/v1/models/${slug}/thumbnail`, name: `${slug}-thumbnail.png`, fallbackType: 'image/png' },
        image,
      ),
  }),

  defineTool({
    name: 'set_model_thumbnail',
    description: "Set a model's thumbnail from a base64 PNG (at most 10 MiB), as a revision in its history.",
    input: z.object({ slug, png_base64: z.string().min(1) }),
    risk: 'write',
    routes: ['PUT /api/v1/models/{slug}/thumbnail'],
    handler: async ({ slug, png_base64 }, ctx) => {
      const form = fileForm(decodeBase64(png_base64, 'png_base64'), 'thumbnail.png', 'image/png')
      return json(
        await command(ctx, `set thumbnail of ${slug}`, (headers) =>
          ctx.backend.PUT('/api/v1/models/{slug}/thumbnail', {
            params: { path: { slug } },
            body: { file: 'thumbnail.png' },
            bodySerializer: () => form,
            headers,
          }),
        ),
      )
    },
  }),

  defineTool({
    name: 'delete_model_thumbnail',
    description: "Remove a model's own thumbnail, as a revision in its history.",
    input: z.object({ slug }),
    risk: 'write',
    routes: ['DELETE /api/v1/models/{slug}/thumbnail'],
    handler: async ({ slug }, ctx) =>
      json(
        await command(ctx, `delete thumbnail of ${slug}`, (headers) =>
          ctx.backend.DELETE('/api/v1/models/{slug}/thumbnail', { params: { path: { slug } }, headers }),
        ),
      ),
  }),
]
