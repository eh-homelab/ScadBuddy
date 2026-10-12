import { randomBytes } from 'node:crypto'
import { z } from 'zod'
import { decodeText, type WorkspaceFileInfo, WorkspaceError, workspaceOf, workspacePath } from '../workspace/store.js'
import { decodeBase64 } from './common.js'
import { ToolError, type ToolContext } from './registry.js'

// Uploads from the assistant's workspace (#2329 part 2): every tool that sends a
// file's bytes or text to ScadBuddy takes `workspace_path` as the alternative to
// its inline content, so a file the assistant downloaded, saved or wrote is sent
// without the model writing it out token by token.
//
// `workspace_sha256` goes with it, required: it is the sha256 the model read the
// file at (workspace_list, workspace_read, workspace_save), and the upload is
// refused when the file is no longer at it. So the tool's input names the exact
// bytes: an approval card and the audit row (both the call's input, scrubbed,
// approvals/service.ts `summariseInput`) show the path and the sha256, never the
// bytes, and an approval binds to that content, not to whatever the path holds
// when the call runs. An approval card also shows the file's size, read from the
// row the sha256 pins (workspace/store.ts `withWorkspaceSize`, #2396), and the
// size is answered in each tool's result.
//
// Binary uploads stream from Postgres (SessionFiles.chunks) into the backend's
// multipart route; text goes as the route's JSON body, decoded as UTF-8 first
// and refused when it is not text.

export const workspacePathInput = z
  .string()
  .min(1)
  .max(512)
  .describe('A file in your workspace (workspace_list lists them) to send instead of inline content')

export const workspaceShaInput = z
  .string()
  .regex(/^[0-9a-f]{64}$/, 'a sha256 as workspace_list, workspace_read or workspace_save gives it')
  .describe(
    "With workspace_path, required: the file's sha256 as workspace_list, workspace_read or workspace_save gave it. " +
      'The upload is refused if the file has changed since',
  )

/** The sentence each upload tool's description ends with. */
export function fromWorkspace(inline: string): string {
  return (
    ` Give ${inline}, or \`workspace_path\` (with its \`workspace_sha256\`) to send a file from your workspace: ` +
    'exactly one of the two. Save a file there first with workspace_save, http_request `save_to_workspace`, a ' +
    'headless-browser download or workspace_write.'
  )
}

type Choice<T> = { inline: T } | { workspace: { path: string; sha256: string } }

/** Which one the call gave: the inline value or a workspace file. Refuses both, neither, or a path without its sha256. */
export function inlineOrWorkspace<T>(
  inline: T | undefined,
  inlineName: string,
  path: string | undefined,
  sha256: string | undefined,
): Choice<T> {
  if ((inline === undefined) === (path === undefined)) {
    throw new ToolError(`give \`${inlineName}\` or \`workspace_path\`: exactly one of the two`)
  }
  if (path === undefined) {
    if (sha256 !== undefined) throw new ToolError('`workspace_sha256` goes with `workspace_path` only')
    return { inline: inline as T }
  }
  if (sha256 === undefined) {
    throw new ToolError('`workspace_path` needs `workspace_sha256`: the sha256 workspace_list or workspace_read gave for the file')
  }
  return { workspace: { path, sha256 } }
}

function storeOf(ctx: ToolContext) {
  if (!ctx.workspace) throw new ToolError('the workspace is not available here: it needs the agent database')
  return { files: ctx.workspace, ws: workspaceOf(ctx) }
}

function asToolError(err: unknown): never {
  if (err instanceof WorkspaceError) throw new ToolError(err.message)
  throw err
}

// CREDENTIALS. workspace_read redacts the agent's secrets (ToolServices.secrets:
// every saved Claude credential, and a classic turn's plugin tokens) from what
// the model sees, but an upload sends the bytes themselves. A secret that reached
// the workspace (an http_request body, a browser download, a saved file) could
// otherwise be moved into a README, a source file or an asset and read back
// unredacted through get_readme or read_file, or reach Bambuddy. So every upload
// is searched for every secret first, the workspace file's bytes and inline
// content alike, text and binary (a byte search), and refused when one is there.
// The refusal names the upload and says a credential was found, never which.

/** The secrets an upload must not carry, as bytes; shorter than four characters is not a secret (secrets.ts `redact`). */
async function secretBytes(ctx: ToolContext): Promise<Buffer[]> {
  const secrets = (await ctx.secrets?.()) ?? []
  return [...new Set(secrets)].filter((s) => s.length >= 4).map((s) => Buffer.from(s, 'utf8'))
}

function holdsSecret(bytes: Uint8Array, secrets: readonly Buffer[]): boolean {
  const haystack = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  return secrets.some((secret) => haystack.indexOf(secret) !== -1)
}

/** The refusal of an upload that carries a credential; it never says which. */
function credentialFound(upload: string, what: string): ToolError {
  return new ToolError(
    `${upload}: refused, ${what} contains one of the agent's credentials (a saved Claude credential or a plugin token), ` +
      'which may never be uploaded. Nothing was sent.',
  )
}

/** Refuses inline bytes or text that carry a credential. */
async function checkInline(ctx: ToolContext, upload: string, field: string, content: Uint8Array | string): Promise<void> {
  const secrets = await secretBytes(ctx)
  if (secrets.length === 0) return
  const bytes = typeof content === 'string' ? Buffer.from(content, 'utf8') : content
  if (holdsSecret(bytes, secrets)) throw credentialFound(upload, `\`${field}\``)
}

/** Refuses a workspace file whose bytes carry a credential, read a slice at a time (a secret across two slices is found too). */
async function checkWorkspaceFile(ctx: ToolContext, upload: string, info: WorkspaceFileInfo): Promise<void> {
  const secrets = await secretBytes(ctx)
  if (secrets.length === 0) return
  const { files, ws } = storeOf(ctx)
  const overlap = Math.max(...secrets.map((s) => s.length)) - 1
  let tail = Buffer.alloc(0)
  for await (const part of files.chunks(ws, info.path, info.sha256)) {
    const window = Buffer.concat([tail, part])
    if (holdsSecret(window, secrets)) throw credentialFound(upload, `the workspace file ${info.path}`)
    tail = window.subarray(Math.max(0, window.length - overlap))
  }
}

/** The workspace file a call names, as it is now: there, at the sha256 the call gave, and holding no credential. */
export async function workspaceFile(ctx: ToolContext, file: { path: string; sha256: string }, upload: string): Promise<WorkspaceFileInfo> {
  const { files, ws } = storeOf(ctx)
  try {
    const info = await files.stat(ws, file.path)
    if (!info) throw new ToolError(`no file ${workspacePath(file.path)} in the workspace; workspace_list lists them`)
    if (info.sha256 !== file.sha256) {
      throw new ToolError(
        `${info.path} changed since it was read: its sha256 is ${info.sha256}, not ${file.sha256}. Read or list it again; nothing was sent`,
      )
    }
    // Sent later by sha256 (`chunks`), so the bytes searched are the bytes sent.
    await checkWorkspaceFile(ctx, upload, info)
    return info
  } catch (err) {
    return asToolError(err)
  }
}

/** A workspace file's text, for a route that takes text: refused unless it is UTF-8 text with no credential in it. */
export async function workspaceText(
  ctx: ToolContext,
  file: { path: string; sha256: string },
  upload: string,
): Promise<{ text: string; info: WorkspaceFileInfo }> {
  const { files, ws } = storeOf(ctx)
  try {
    const read = await files.read(ws, file.path)
    if (!read) throw new ToolError(`no file ${workspacePath(file.path)} in the workspace; workspace_list lists them`)
    if (read.sha256 !== file.sha256) {
      throw new ToolError(
        `${read.path} changed since it was read: its sha256 is ${read.sha256}, not ${file.sha256}. Read or list it again; nothing was sent`,
      )
    }
    if (holdsSecret(read.bytes, await secretBytes(ctx))) throw credentialFound(upload, `the workspace file ${read.path}`)
    // The bytes decide, not the stored type: a .scad saved as octet-stream is still text.
    const text = decodeText(read.bytes)
    if (text === undefined) throw new ToolError(`${read.path} is not UTF-8 text (${read.mediaType}); nothing was sent`)
    const { bytes: _bytes, ...info } = read
    return { text, info }
  } catch (err) {
    return asToolError(err)
  }
}

/** A multipart field value as a FormData serialiser quotes it (WHATWG: `"`, CR and LF escaped). */
function quoted(value: string): string {
  return value.replace(/"/g, '%22').replace(/\r/g, '%0D').replace(/\n/g, '%0A')
}

export type StreamedForm = {
  body: ReadableStream<Uint8Array>
  /** `multipart/form-data; boundary=…`, the request's Content-Type. */
  contentType: string
}

/**
 * A multipart body whose `part` is a workspace file streamed from Postgres a
 * slice at a time, after the text `fields`. Built anew for each send (a command
 * re-sends after an answer that never came, tools/command.ts), since a stream
 * is read once. The file must still be at `info.sha256`, or the stream fails
 * and the request with it.
 */
export function streamedForm(
  ctx: ToolContext,
  info: WorkspaceFileInfo,
  part: { name: string; filename: string; type: string },
  fields: Record<string, string> = {},
): StreamedForm {
  const { files, ws } = storeOf(ctx)
  const boundary = `----scadbuddy-${randomBytes(12).toString('hex')}`
  const encoder = new TextEncoder()
  const head =
    Object.entries(fields)
      .map(([name, value]) => `--${boundary}\r\nContent-Disposition: form-data; name="${quoted(name)}"\r\n\r\n${value}\r\n`)
      .join('') +
    `--${boundary}\r\nContent-Disposition: form-data; name="${quoted(part.name)}"; filename="${quoted(part.filename)}"\r\n` +
    `Content-Type: ${part.type}\r\n\r\n`
  const tail = `\r\n--${boundary}--\r\n`
  async function* parts(): AsyncGenerator<Uint8Array> {
    yield encoder.encode(head)
    yield* files.chunks(ws, info.path, info.sha256)
    yield encoder.encode(tail)
  }
  const iterator = parts()
  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const next = await iterator.next()
        if (next.done) controller.close()
        else controller.enqueue(next.value)
      } catch (err) {
        controller.error(err)
      }
    },
    async cancel() {
      await iterator.return(undefined)
    },
  })
  return { body, contentType: `multipart/form-data; boundary=${boundary}` }
}

/** A file an upload sends: bytes given inline, or a workspace file checked by `workspaceFile`. */
export type UploadSource = { bytes: Uint8Array<ArrayBuffer> } | { info: WorkspaceFileInfo }

/**
 * The openapi-fetch options of ONE send of a multipart upload, to spread into
 * the call: inline bytes as FormData, or a workspace file streamed
 * (`streamedForm`, with `duplex: 'half'`, which a streamed request body needs).
 * Call it inside the send, so a re-send gets a fresh stream.
 */
export function uploadInit<H extends Record<string, string>>(
  ctx: ToolContext,
  source: UploadSource,
  part: { name: string; filename: string; type: string },
  headers: H,
  fields: Record<string, string> = {},
): { bodySerializer: () => FormData | ReadableStream<Uint8Array>; headers: H | (H & { 'Content-Type': string }); duplex?: 'half' } {
  if ('bytes' in source) {
    const form = new FormData()
    for (const [name, value] of Object.entries(fields)) form.append(name, value)
    form.append(part.name, new Blob([source.bytes], { type: part.type }), part.filename)
    return { bodySerializer: () => form, headers }
  }
  const streamed = streamedForm(ctx, source.info, part, fields)
  return { bodySerializer: () => streamed.body, headers: { ...headers, 'Content-Type': streamed.contentType }, duplex: 'half' }
}

/** The file a binary upload sends: its base64 decoded, or the workspace file, checked. */
export async function binarySource(
  ctx: ToolContext,
  args: { upload: string; base64: string | undefined; base64Name: string; path: string | undefined; sha256: string | undefined },
): Promise<UploadSource> {
  const choice = inlineOrWorkspace(args.base64, args.base64Name, args.path, args.sha256)
  if ('inline' in choice) {
    const bytes = decodeBase64(choice.inline, args.base64Name)
    await checkInline(ctx, args.upload, args.base64Name, bytes)
    return { bytes }
  }
  return { info: await workspaceFile(ctx, choice.workspace, args.upload) }
}

/** The text a text upload sends: given inline, or the workspace file's, decoded. `info` names the file it came from. */
export async function textSource(
  ctx: ToolContext,
  args: { upload: string; text: string | undefined; textName: string; path: string | undefined; sha256: string | undefined },
): Promise<{ text: string; info?: WorkspaceFileInfo }> {
  const choice = inlineOrWorkspace(args.text, args.textName, args.path, args.sha256)
  if ('inline' in choice) {
    await checkInline(ctx, args.upload, args.textName, choice.inline)
    return { text: choice.inline }
  }
  return workspaceText(ctx, choice.workspace, args.upload)
}

/** What a result says about the workspace file it sent. */
export function sent(info: WorkspaceFileInfo) {
  return { path: info.path, size_bytes: info.size, sha256: info.sha256, media_type: info.mediaType }
}

/**
 * A command's answer with the workspace file it sent (`workspace_file`), so the
 * result, and the audit row's detail, name its path, size and sha256. An answer
 * that is not an object (none, or a list) is wrapped as `{ result }`.
 */
export function withSent(answer: unknown, info: WorkspaceFileInfo | undefined): unknown {
  if (info === undefined) return answer
  const body = typeof answer === 'object' && answer !== null && !Array.isArray(answer) ? answer : answer === undefined ? {} : { result: answer }
  return { ...body, workspace_file: sent(info) }
}

/** The file name a workspace path uploads as: its last part. */
export function uploadName(path: string): string {
  return path.split('/').at(-1) ?? path
}
