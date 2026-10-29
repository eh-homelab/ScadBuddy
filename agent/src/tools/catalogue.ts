import { z } from 'zod'
import { binary } from './binary.js'
import { staleBase } from './authoring.js'
import { command, commandAnswer, isRunning } from './command.js'
import { ok } from './call.js'
import { commit, slug } from './common.js'
import { defineTool, image, json, text, type Tool } from './registry.js'
import { backendPage, PAGED, pageInput, totalCount } from './pagination.js'
import {
  fromWorkspace,
  FROM_WORKSPACE_ONLY,
  inlineOrWorkspace,
  textSource,
  uploadInit,
  uploadName,
  withSent,
  workspacePathInput,
  workspaceShaInput,
  workspaceFile,
  workspaceText,
} from './workspaceUploads.js'

/** A model's display name from a workspace file's name: "box_v2.scad" is "box_v2". */
function modelNameOf(path: string): string {
  return uploadName(path).replace(/\.scad$/i, '') || 'model'
}

// Catalogue & models (issue #251): list, get, create, import, check, duplicate,
// delete, and edit details, README, source and thumbnail. Routes:
// backend/scadbuddy/api/models.py.

export const catalogueTools: Tool[] = [
  defineTool({
    name: 'list_models',
    label: 'List models',
    description:
      'List the models in the catalogue (bundled templates and user models) with name, slug, tags, ' +
      'version, origin and upstream state.' + PAGED,
    input: z.object({ ...pageInput }),
    risk: 'read',
    source:
      'model metadata (names, descriptions, tags) written by model authors or imported from the web',
    routes: ['GET /api/v1/models'],
    title: () => 'List templates',
    // The backend pages (#843): it builds only this page's records, not the catalogue.
    handler: async (args, { backend }) =>
      json(
        await backendPage(args, (m: { slug: string }) => m.slug, 'list_models', async (query) => {
          const pending = backend.GET('/api/v1/models', { params: { query } })
          return { items: await ok(pending, 'list models'), total: totalCount((await pending).response) }
        }),
      ),
  }),

  defineTool({
    name: 'get_model',
    label: "Read a model's details",
    description: "One model's metadata: name, description, tags, current version, libraries and upstream.",
    input: z.object({ slug }),
    risk: 'read',
    source:
      'model metadata (names, descriptions, tags) written by model authors or imported from the web',
    routes: ['GET /api/v1/models/{slug}'],
    title: ({ slug }) => `Read template → ${slug}`,
    handler: async ({ slug }, { backend }) =>
      json(await ok(backend.GET('/api/v1/models/{slug}', { params: { path: { slug } } }), `get model ${slug}`)),
  }),

  defineTool({
    name: 'get_source',
    label: "Read a model's source",
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
    label: 'Check OpenSCAD source for errors',
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
    label: 'Create a model',
    description:
      'Add a new model from OpenSCAD source. The slug is derived from the name. Refused when the source ' +
      'fails the parse check unless `force` is true.' +
      fromWorkspace('the `source`'),
    input: z.object({
      name: z.string().min(1),
      source: z.string().max(1_000_000).optional().describe('The OpenSCAD source'),
      workspace_path: workspacePathInput.optional(),
      workspace_sha256: workspaceShaInput.optional(),
      description: z.string().default(''),
      tags: z.array(z.string()).default([]),
      force: z.boolean().default(false),
    }),
    risk: 'write',
    routes: ['POST /api/v1/models'],
    title: ({ name }) => `Create template → ${name}`,
    handler: async ({ source, workspace_path, workspace_sha256, ...rest }, ctx) => {
      const { text, info } = await textSource(ctx, { upload: 'create_model', text: source, textName: 'source', path: workspace_path, sha256: workspace_sha256 })
      const body = { ...rest, source: text }
      return json(withSent(await command(ctx, `create model ${rest.name}`, (headers) => ctx.backend.POST('/api/v1/models', { body, headers })), info))
    },
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
    label: 'Import a model from a URL',
    description:
      'Import a model from an https URL to its .scad source. The backend fetches the URL, so this needs a ' +
      'human approval. Or import a .scad file you already hold: `workspace_path` (with its `workspace_sha256`) ' +
      'instead of `url` creates the model from that file as create_model does, under the same approval; exactly ' +
      'one of the two.',
    input: z.object({
      url: z.string().url().max(2048).optional(),
      workspace_path: workspacePathInput.optional(),
      workspace_sha256: workspaceShaInput.optional(),
      name: z.string().optional().describe('Display name; taken from the file name when omitted'),
      force: z.boolean().default(false),
    }),
    risk: 'outward',
    // From a workspace file it creates the model through `POST /api/v1/models`, create_model's
    // route, which create_model lists.
    routes: ['POST /api/v1/models/import'],
    summarize: ({ url, workspace_path, workspace_sha256, name }) =>
      url !== undefined
        ? `Fetch and import a model from ${url}${name ? ` as "${name}"` : ''}`
        : `Import a model from the workspace file ${workspace_path ?? '?'} (sha256 ${workspace_sha256 ?? '?'})${name ? ` as "${name}"` : ''}`,
    handler: async ({ url, workspace_path, workspace_sha256, name, force }, ctx) => {
      const choice = inlineOrWorkspace(url, 'url', workspace_path, workspace_sha256)
      if ('inline' in choice) {
        return json(
          await command(ctx, `import ${choice.inline}`, (headers) =>
            ctx.backend.POST('/api/v1/models/import', { body: { url: choice.inline, name: name ?? null, force }, headers }),
          ),
        )
      }
      const { text, info } = await workspaceText(ctx, choice.workspace, 'import_model')
      const body = { name: name ?? modelNameOf(info.path), source: text, description: '', tags: [], force }
      return json(withSent(await command(ctx, `import ${info.path}`, (headers) => ctx.backend.POST('/api/v1/models', { body, headers })), info))
    },
  }),

  defineTool({
    name: 'duplicate_model',
    label: 'Duplicate a model',
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
    label: "Edit a model's name, description or tags",
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
    label: "Replace a model's source",
    description:
      "Replace a model's OpenSCAD source as one revision in its history (undo with restore_version). " +
      'Refused when the parse check fails unless `force` is true. Pass `base` (the `version` you read ' +
      'the source at) to have it refused if someone else saved since; for a small change, apply_patch.',
    input: z.object({
      slug,
      source: z.string().max(1_000_000),
      message: z.string().max(200).optional().describe('What the revision is called in the history'),
      force: z.boolean().default(false),
      base: commit
        .optional()
        .describe('Refuse as a conflict naming the `current` revision unless the model is still here'),
    }),
    risk: 'write',
    routes: ['PUT /api/v1/models/{slug}/source'],
    title: ({ slug, message }) => `Edit source → ${slug}${message ? `: ${message}` : ''}`,
    handler: async ({ slug, source, message, force, base }, ctx) => {
      const answered = await commandAnswer(ctx, `update source of ${slug}`, (headers) =>
        ctx.backend.PUT('/api/v1/models/{slug}/source', {
          params: { path: { slug } },
          body: { source, message: message ?? null, force, base: base ?? null },
          headers,
        }),
      )
      if (isRunning(answered)) return json(answered)
      const stale = staleBase(
        answered,
        base,
        'Nothing was written: the model changed after you read it. Call get_source (and get_model ' +
          'for its version) again, redo the change against that, and pass `current` as `base`.',
      )
      if (stale !== null) return stale
      return json(await ok(Promise.resolve(answered), `update source of ${slug}`))
    },
  }),

  defineTool({
    name: 'delete_model',
    label: 'Delete a model',
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
    label: "Read a model's README",
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
    label: "Write a model's README",
    description: "Set a model's README (Markdown), as a revision in its history." + fromWorkspace('the `content`'),
    input: z.object({
      slug,
      content: z.string().max(1_000_000).optional().describe('The Markdown'),
      workspace_path: workspacePathInput.optional(),
      workspace_sha256: workspaceShaInput.optional(),
    }),
    risk: 'write',
    routes: ['PUT /api/v1/models/{slug}/readme'],
    handler: async ({ slug, content, workspace_path, workspace_sha256 }, ctx) => {
      const { text, info } = await textSource(ctx, { upload: 'set_readme', text: content, textName: 'content', path: workspace_path, sha256: workspace_sha256 })
      const answer = await command(ctx, `set README of ${slug}`, (headers) =>
        ctx.backend.PUT('/api/v1/models/{slug}/readme', { params: { path: { slug } }, body: { content: text }, headers }),
      )
      return json(withSent(answer, info))
    },
  }),

  defineTool({
    name: 'delete_readme',
    label: "Delete a model's README",
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
    label: "See a model's thumbnail",
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
    label: "Set a model's thumbnail",
    description: "Set a model's thumbnail from a PNG (at most 10 MiB), as a revision in its history." + FROM_WORKSPACE_ONLY,
    input: z
      .object({
        slug,
        workspace_path: workspacePathInput,
        workspace_sha256: workspaceShaInput,
      })
      .strict(),
    risk: 'write',
    routes: ['PUT /api/v1/models/{slug}/thumbnail'],
    handler: async ({ slug, workspace_path, workspace_sha256 }, ctx) => {
      const info = await workspaceFile(ctx, { path: workspace_path, sha256: workspace_sha256 }, 'set_model_thumbnail')
      const answer = await command(ctx, `set thumbnail of ${slug}`, (headers) =>
        ctx.backend.PUT('/api/v1/models/{slug}/thumbnail', {
          params: { path: { slug } },
          body: { file: 'thumbnail.png' },
          ...uploadInit(ctx, info, { name: 'file', filename: 'thumbnail.png', type: 'image/png' }, headers),
        }),
      )
      return json(withSent(answer, info))
    },
  }),

  defineTool({
    name: 'add_model_media',
    label: 'Add an image or video to a model',
    description:
      "Add an image (PNG, JPEG or WebP, at most 10 MB) or a video (MP4 or WebM) to a model's media gallery, as its " +
      'last item and one revision; the type comes from the bytes. Answers the model, whose `media` lists the items.' +
      FROM_WORKSPACE_ONLY,
    input: z
      .object({
        slug,
        workspace_path: workspacePathInput,
        workspace_sha256: workspaceShaInput,
        filename: z.string().min(1).max(255).optional().describe('e.g. "photo.jpg"; the workspace file\'s own name by default'),
        caption: z.string().max(500).optional().describe("The item's caption"),
      })
      .strict(),
    risk: 'write',
    routes: ['POST /api/v1/models/{slug}/media'],
    title: ({ slug, filename, workspace_path }) => `Add media ${filename ?? workspace_path} → ${slug}`,
    handler: async ({ slug, workspace_path, workspace_sha256, filename, caption }, ctx) => {
      const info = await workspaceFile(ctx, { path: workspace_path, sha256: workspace_sha256 }, 'add_model_media')
      const name = filename ?? uploadName(info.path)
      const answer = await command(ctx, `add media to ${slug}`, (headers) =>
        ctx.backend.POST('/api/v1/models/{slug}/media', {
          params: { path: { slug } },
          body: { file: name, caption: caption ?? null, poster: null },
          ...uploadInit(ctx, info, { name: 'file', filename: name, type: info.mediaType }, headers, caption === undefined ? {} : { caption }),
        }),
      )
      return json(withSent(answer, info))
    },
  }),

  defineTool({
    name: 'delete_model_thumbnail',
    label: "Remove a model's thumbnail",
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
