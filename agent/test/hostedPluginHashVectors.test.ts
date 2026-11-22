import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { hashTree } from '../src/plugins/packages/hash.js'

// A hosted plugin's version (#6932) records the content hash of its zip's unpacked tree,
// computed by the backend (backend/scadbuddy/hosted_plugins/bundle.py `content_hash`) and
// checked here by the package cache against `hashTree`, as for a git pin. These vectors pin
// the two together: each case is a tree written to disk and hashed by hashTree, and
// backend/tests/hosted_plugins/test_hash_vectors.py checks every one against its copy in
// backend/tests/fixtures. Regenerate (then copy it there):
// UPDATE_HOSTED_PLUGIN_HASH_VECTORS=1 pnpm exec vitest run test/hostedPluginHashVectors.test.ts
const FILE = new URL('./fixtures/hosted-plugin-hash-vectors.json', import.meta.url)

type Input = { path: string; text: string; executable?: boolean }

const CASES: Array<{ name: string; files: Input[] }> = [
  {
    name: 'one skill and the generated manifest',
    files: [
      { path: '.claude-plugin/plugin.json', text: '{"name":"greeter","description":"Says hello","version":"1.0.0"}\n' },
      { path: 'skills/hello/SKILL.md', text: '---\nname: hello\ndescription: Says hello\n---\n\nSay hello.\n' },
    ],
  },
  {
    name: 'an executable script, an agent, a command and an empty file',
    files: [
      { path: '.claude-plugin/plugin.json', text: '{"name":"tools","description":"","version":"3.0.0"}\n' },
      { path: 'skills/run/SKILL.md', text: '---\nname: run\ndescription: Runs it\n---\n' },
      { path: 'skills/run/scripts/go.sh', text: '#!/bin/sh\necho go\n', executable: true },
      { path: 'agents/review.md', text: '---\nname: review\ndescription: Reviews\n---\n' },
      { path: 'commands/wave.md', text: 'Wave.\n' },
      { path: 'skills/run/empty.txt', text: '' },
    ],
  },
  {
    // UTF-16 code unit order (JavaScript's sort) puts U+1F600 (D83D DE00) before U+FF5E;
    // code point order puts it after. The hash takes the first.
    name: 'paths whose order differs between UTF-16 and code points',
    files: [
      { path: 'skills/a/～.md', text: 'tilde\n' },
      { path: 'skills/a/\u{1f600}.md', text: 'smile\n' },
      { path: 'skills/a/Z.md', text: 'upper\n' },
      { path: 'skills/a/a.md', text: 'lower\n' },
      { path: 'skills/a-b/SKILL.md', text: 'dash\n' },
      { path: 'skills/a/SKILL.md', text: 'slash\n' },
    ],
  },
]

describe('hosted plugin hash vectors', () => {
  it('match what hashTree computes for each tree', async () => {
    const out = []
    for (const c of CASES) {
      const root = mkdtempSync(path.join(tmpdir(), 'hosted-hash-'))
      try {
        for (const f of c.files) {
          const abs = path.join(root, ...f.path.split('/'))
          mkdirSync(path.dirname(abs), { recursive: true })
          writeFileSync(abs, f.text)
          chmodSync(abs, f.executable ? 0o755 : 0o644)
        }
        const tree = await hashTree(root)
        out.push({ name: c.name, files: c.files.map((f) => ({ path: f.path, text: f.text, executable: f.executable ?? false })), hash: tree.hash, file_list: tree.files })
      } finally {
        rmSync(root, { recursive: true, force: true })
      }
    }
    const text = `${JSON.stringify({ cases: out }, null, 2)}\n`
    if (process.env.UPDATE_HOSTED_PLUGIN_HASH_VECTORS === '1') writeFileSync(FILE, text)
    expect(readFileSync(FILE, 'utf8')).toBe(text)
  })
})
