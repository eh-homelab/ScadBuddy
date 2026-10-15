import { describe, expect, it } from 'vitest'
import { type FileToolContext, grep } from '../src/builtins/files.js'
import type { SessionFiles, WorkspaceFileInfo } from '../src/workspace/store.js'

// Grep against an in-memory workspace, so its matching is covered without Postgres
// (builtins.pg.test.ts runs it through the real store and the turn tools).
function workspace(files: Record<string, string>): FileToolContext {
  const info = (path: string): WorkspaceFileInfo => ({
    path,
    mediaType: 'text/plain',
    size: files[path]!.length,
    sha256: '',
    source: null,
    createdAt: new Date(0),
    updatedAt: new Date(0),
  })
  const store = {
    list: async () => Object.keys(files).map(info),
    stat: async (_ws: unknown, path: string) => (path in files ? info(path) : null),
    read: async (_ws: unknown, path: string) => (path in files ? { ...info(path), bytes: Buffer.from(files[path]!) } : null),
  }
  return { files: store as unknown as SessionFiles, ws: { kind: 'session', sessionId: 's' } }
}

const ctx = workspace({ 'a.txt': 'hello\nworld\nhello again\n' })

describe('Grep matches line by line, as ripgrep does', () => {
  it('anchors ^ and $ at every line', async () => {
    expect((await grep(ctx, { pattern: '^world', output_mode: 'content' })).text).toBe('a.txt:2:world')
    expect((await grep(ctx, { pattern: 'again$', output_mode: 'content' })).text).toBe('a.txt:3:hello again')
  })

  it('counts a zero-length match once per line, not after the final newline', async () => {
    expect((await grep(ctx, { pattern: '^', output_mode: 'count' })).text).toBe('a.txt:3\n\nFound 3 total occurrences across 1 file.')
    expect((await grep(workspace({ 'n.txt': 'a\nb' }), { pattern: '^', output_mode: 'count' })).text).toBe('n.txt:2\n\nFound 2 total occurrences across 1 file.')
    const crlf = workspace({ 'w.txt': 'hello\r\nworld\r\n' })
    expect((await grep(crlf, { pattern: '^', output_mode: 'count' })).text).toBe('w.txt:2\n\nFound 2 total occurrences across 1 file.')
    // As plain ripgrep (no --crlf): $ is the end of the line, after its \r.
    expect((await grep(crlf, { pattern: 'hello$', output_mode: 'content' })).text).toBe('No matches found')
  })

  it('never crosses a newline unless multiline is set', async () => {
    expect((await grep(ctx, { pattern: 'hello\\s+world', output_mode: 'content' })).text).toBe('No matches found')
    expect((await grep(ctx, { pattern: 'world[^x]+again', output_mode: 'content' })).text).toBe('No matches found')
    expect((await grep(ctx, { pattern: 'hello\\s+world', output_mode: 'content', multiline: true })).text).toBe('a.txt:1:hello\na.txt:2:world')
  })
})
