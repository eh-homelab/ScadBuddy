import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { ok } from './call.js'
import type { ToolServices } from './registry.js'

// Binary tool results (3MF, GLB, PNG/SVG): inline when small, a LINK when not.
//
// A tool result travels as one JSON-RPC message, base64-inflated by a third,
// so inlining has a cap (`ToolServices.maxInlineBytes`, 8 MiB by default).
// Over the cap the call does not fail: it answers with the size, the media
// type and where to fetch the bytes, as an MCP `resource_link` content item
// (MCP spec 2025-06-18, Server / Tools, "Resource Links": "A tool MAY return
// links to Resources, to provide additional context or data. In this case, the
// tool will return a URI that can be subscribed to or fetched by the client",
// and "Resource links returned by tools are not guaranteed to appear in the
// results of a `resources/list` request";
// https://modelcontextprotocol.io/specification/2025-06-18/server/tools#resource-links).
// The pinned @modelcontextprotocol/sdk (1.30.1, types.d.ts ResourceLinkSchema)
// types it as { type: 'resource_link', uri, name, mimeType?, size?, description? }.
//
// There are no ScadBuddy MCP resources yet (#264), so the link is the backend
// route itself: absolute under SCADBUDDY_PUBLIC_URL when that is set (the
// ingress sends /api/v1/* to the backend, spec §4.2), else the bare path with
// a note saying so. When #264 lands this becomes a `scadbuddy://` resource.
//
// The harness projection gets the same link: the harness runs with `tools: []`
// (spec D7), so a file written to its work directory could not be read by the
// model anyway.
//
// The body is read as a stream and abandoned at the cap, so an oversized file
// is never buffered whole just to be refused.

export const DEFAULT_MAX_INLINE_BYTES = 8 * 1024 * 1024

type Pending = Promise<{ data?: unknown; error?: unknown; response: Response }>

export type BinaryRef = {
  /** The backend route that serves the bytes, e.g. `/api/v1/outputs/abc/model.3mf`. */
  path: string
  /** A human-readable name for the link. */
  name: string
  /** Used when the backend sends no Content-Type. */
  fallbackType: string
}

type Read = { inline: true; bytes: ArrayBuffer } | { inline: false; size: number | undefined }

async function readCapped(stream: ReadableStream<Uint8Array> | null, declared: number | undefined, cap: number): Promise<Read> {
  if (declared !== undefined && declared > cap) {
    // Not awaited: a cancel can wait on the peer; the bytes are abandoned either way.
    void stream?.cancel().catch(() => {})
    return { inline: false, size: declared }
  }
  if (!stream) return { inline: true, bytes: new ArrayBuffer(0) }
  const reader = stream.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    total += value.byteLength
    if (total > cap) {
      void reader.cancel().catch(() => {})
      return { inline: false, size: declared }
    }
    chunks.push(value)
  }
  const bytes = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.byteLength
  }
  return { inline: true, bytes: bytes.buffer }
}

export function linkResult(
  ref: BinaryRef,
  mimeType: string,
  size: number | undefined,
  services: Pick<ToolServices, 'publicBaseUrl' | 'maxInlineBytes'>,
): CallToolResult {
  const cap = services.maxInlineBytes ?? DEFAULT_MAX_INLINE_BYTES
  const base = services.publicBaseUrl?.replace(/\/+$/, '')
  const uri = base ? `${base}${ref.path}` : ref.path
  const sizeText = size === undefined ? `more than ${cap} bytes` : `${size} bytes`
  return {
    content: [
      {
        type: 'resource_link',
        uri,
        name: ref.name,
        mimeType,
        ...(size === undefined ? {} : { size }),
        description: `Too large to return inline (${sizeText}; the limit is ${cap}). Fetch it with GET ${uri}.`,
      },
      {
        type: 'text',
        text: JSON.stringify(
          {
            inline: false,
            reason: `larger than the ${cap}-byte inline limit`,
            size_bytes: size ?? null,
            mime_type: mimeType,
            fetch: { method: 'GET', path: ref.path, ...(base ? { url: uri } : {}) },
            note: base
              ? 'Served by ScadBuddy at its public URL. An MCP resource for this arrives with #264.'
              : 'A path on the ScadBuddy backend (its public URL is not configured here: ' +
                'SCADBUDDY_PUBLIC_URL). An MCP resource for this arrives with #264.',
          },
          null,
          2,
        ),
      },
    ],
  }
}

/**
 * Fetches binary content and renders it inline with `render` when it fits,
 * otherwise returns a link. Call with `parseAs: 'stream'`.
 */
export async function binary(
  pending: Pending,
  what: string,
  services: Pick<ToolServices, 'publicBaseUrl' | 'maxInlineBytes'>,
  ref: BinaryRef,
  render: (bytes: ArrayBuffer, mimeType: string) => CallToolResult,
): Promise<CallToolResult> {
  const result = await pending
  const stream = (await ok(Promise.resolve(result), what)) as ReadableStream<Uint8Array> | null
  const mimeType = result.response.headers.get('content-type')?.split(';')[0]?.trim() || ref.fallbackType
  const lengthHeader = result.response.headers.get('content-length')
  const declared = lengthHeader !== null && /^\d+$/.test(lengthHeader) ? Number(lengthHeader) : undefined
  const read = await readCapped(stream, declared, services.maxInlineBytes ?? DEFAULT_MAX_INLINE_BYTES)
  return read.inline ? render(read.bytes, mimeType) : linkResult(ref, mimeType, read.size, services)
}
