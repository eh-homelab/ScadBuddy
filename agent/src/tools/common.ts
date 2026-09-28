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

export const outputId = z.string().min(1).describe('Output id, as list_outputs returns it')

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

export const paramValue =z.union([z.boolean(), z.number(), z.string()])

// `catchall`, not `z.record`: the MCP server bundled in @anthropic-ai/claude-agent-sdk
// 0.3.283 fails `tools/list` with "Cannot read properties of undefined (reading
// 'push')" for any tool whose input has a `z.record` field (measured 2026-09-27;
// test/projections.test.ts lists every tool through it). The JSON Schema is the
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
