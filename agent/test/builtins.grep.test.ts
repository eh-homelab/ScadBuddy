import { describe, expect, it } from 'vitest'
import { type FileToolContext, grep } from '../src/builtins/files.js'
import type { SessionFiles, WorkspaceFileInfo } from '../src/workspace/store.js'

// Grep against an in-memory workspace, so its matching is covered without Postgres
// (builtins.pg.test.ts runs it through the real store and the turn tools).
function workspace(files: Record<string, string>): FileToolContext {
  const info = (path: string): WorkspaceFileInfo => ({
    path,
    mediaType: 'text/plain',
    size: Buffer.byteLength(files[path]!),
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

  it('maps a per-line hit back to its line for -o and context', async () => {
    expect((await grep(ctx, { pattern: 'again$', output_mode: 'content', '-o': true })).text).toBe('a.txt:3:again')
    expect((await grep(ctx, { pattern: '^world', output_mode: 'content', '-A': 1 })).text).toBe('a.txt:2:world\na.txt-3-hello again')
  })

  it('refuses a newline in the pattern unless multiline is set, as ripgrep does', async () => {
    for (const pattern of ['hello\\nworld', 'hello[\\n]', 'hello\nworld']) {
      expect(await grep(ctx, { pattern, output_mode: 'content' })).toMatchObject({
        isError: true,
        text: expect.stringContaining('set multiline: true'),
      })
    }
    // An escaped backslash before n is not a newline.
    expect((await grep(ctx, { pattern: 'hello\\\\n', output_mode: 'content' })).text).toBe('No matches found')
    // Every other way of writing a newline is refused too, and one in a positive class
    // inside what looks like a negated one ([a[^\n]] is the class [a[^\n] then "]").
    for (const pattern of ['a\\x0ab', 'a\\u000Ab', 'a\\cJb', 'a\\012b', 'a\\12b', 'a\\0123', 'a\\128', '[a[^\\n]]']) {
      expect(await grep(ctx, { pattern, output_mode: 'content' })).toMatchObject({
        isError: true,
        text: expect.stringContaining('set multiline: true'),
      })
    }
    // \123 is octal S, not \12 then 3: the lookahead stops only at another octal digit.
    expect((await grep(ctx, { pattern: 'a\\123', output_mode: 'content' })).text).toBe('No matches found')
    // \N is the letter N, not a newline.
    expect((await grep(ctx, { pattern: 'a\\Nb', output_mode: 'content' })).text).toBe('No matches found')
    // An escaped [ starts no class, so a\[^x]n is literal text, not a newline.
    expect((await grep(ctx, { pattern: 'a\\[^x]n', output_mode: 'content' })).text).toBe('No matches found')
    // multiline lifts the refusal and matches across lines.
    expect((await grep(ctx, { pattern: 'hello\\nworld', output_mode: 'content', multiline: true })).text).toBe('a.txt:1:hello\na.txt:2:world')
    // A negated class never matches a newline, so ripgrep allows it.
    expect((await grep(ctx, { pattern: '^hello[^\\n]*', output_mode: 'content' })).text).toBe('a.txt:1:hello\na.txt:3:hello again')
  })
})
