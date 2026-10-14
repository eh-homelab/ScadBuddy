import { z } from 'zod'
import { ToolError } from './registry.js'

// Argument schemas several tool groups share. Patterns mirror the backend's own
// path-parameter validation in backend/openapi.json, so a bad value is refused
// here with a clear message instead of as a backend 422.

export const slug = z
  .string()
  .regex(/^(builtin:)?[a-z0-9][a-z0-9-]*$/)
  .max(108)
  .describe('Model slug, e.g. "keychain" or "builtin:gridfinity-bin" for a bundled template')

/** Output ids are 32 lowercase hex digits (every `output_id` path parameter in backend/openapi.json). */
export const outputId = z
  .string()
  .regex(/^[0-9a-f]{32}$/, 'must be an output id: 32 lowercase hex digits, as list_outputs returns it')
  .describe('Output id, as list_outputs returns it')

export const commit = z.string().regex(/^[0-9a-f]{7,40}$/).describe('A revision id from list_versions')

/** The named views the backend draws (`views/{view}.png` in backend/openapi.json). */
export const VIEW = z.enum(['iso', 'front', 'back', 'left', 'right', 'top', 'bottom'])
export const VIEW_SIZE = z
  .number()
  .int()
  .min(64)
  .max(1024)
  .optional()
  .describe('Edge of the square PNG in pixels (512 by default)')

/**
 * An explicit camera on top of a named view (#830), as the view routes take it
 * (`view_camera` in backend/scadbuddy/api/jobs.py, ranges from render/thumbnail.py).
 */
export const CAMERA = {
  azimuth: z
    .number()
    .min(-360)
    .max(360)
    .optional()
    .describe(
      'Degrees around +Z (Z is up, model mm) from the front: 0 stands at -Y looking +Y (front), 90 at +X ' +
        '(right), 180 at +Y (back), -90 at -X (left); counter-clockwise seen from above. Left out, the ' +
        "named view's (iso is about 28)",
    ),
  elevation: z
    .number()
    .min(-90)
    .max(90)
    .optional()
    .describe("Degrees above the XY plane: 90 looks straight down, -90 straight up. Left out, the named view's (iso is about 49)"),
  zoom: z
    .number()
    .min(1)
    .max(64)
    .optional()
    .describe('Magnification of the fitted frame: 1 (default) shows the whole model around the target, 2 half its width'),
  target: z
    .object({ x: z.number().optional(), y: z.number().optional(), z: z.number().optional() })
    .optional()
    .describe("The point in model mm the view centres and zooms on; a coordinate left out is the bounding box centre's"),
}

export type Camera = {
  azimuth?: number | undefined
  elevation?: number | undefined
  zoom?: number | undefined
  target?: { x?: number | undefined; y?: number | undefined; z?: number | undefined } | undefined
}

/** A camera as the routes' query parameters, leaving out what was not given. */
export function cameraQuery(camera: Camera): {
  azimuth?: number
  elevation?: number
  zoom?: number
  target_x?: number
  target_y?: number
  target_z?: number
} {
  const all = {
    azimuth: camera.azimuth,
    elevation: camera.elevation,
    zoom: camera.zoom,
    target_x: camera.target?.x,
    target_y: camera.target?.y,
    target_z: camera.target?.z,
  }
  return Object.fromEntries(Object.entries(all).filter(([, v]) => v !== undefined))
}

/** `path` with `query`'s defined values as its query string, for a resource link. */
export function withQuery(path: string, query: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) if (value !== undefined) search.set(key, String(value))
  const text = search.toString()
  return text ? `${path}?${text}` : path
}

/** The camera as it was asked for, for a result to repeat; undefined when none was. */
export function cameraOf(camera: Camera): Camera | undefined {
  const asked = Object.fromEntries(Object.entries(camera).filter(([, v]) => v !== undefined))
  return Object.keys(asked).length ? asked : undefined
}

export const paramValue =z.union([z.boolean(), z.number(), z.string()])

// `catchall`, not `z.record`: the MCP server bundled in @anthropic-ai/claude-agent-sdk
// 0.3.283 fails `tools/list` with "Cannot read properties of undefined (reading
// 'push')" for any tool whose input has a `z.record` field (measured 2026-09-27;
// 0.3.287 lists one, #1540; test/projections.test.ts lists every tool through
// it). The JSON Schema is the
// same object-with-additionalProperties either way.
export const params = z
  .object({})
  .catchall(paramValue)
  .describe('Customizer values by parameter name; omitted parameters keep their defaults')

export function decodeBase64(value: string, what: string): Uint8Array<ArrayBuffer> {
  // Node's decoder silently drops characters outside the alphabet, so check the
  // shape first rather than upload silently truncated bytes.
  const compact = value.replace(/\s+/g, '')
  if (compact.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(compact)) {
    throw new ToolError(`${what} is not valid base64`)
  }
  const bytes = Buffer.from(compact, 'base64')
  if (bytes.byteLength === 0) throw new ToolError(`${what} is empty or not base64`)
  return new Uint8Array(bytes)
}

/** A multipart body with one `file` part, for the backend's upload routes. */
export function fileForm(bytes: Uint8Array<ArrayBuffer>, filename: string, type: string): FormData {
  const form = new FormData()
  form.append('file', new Blob([bytes], { type }), filename)
  return form
}
