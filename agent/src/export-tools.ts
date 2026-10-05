import path from 'node:path'
import { writeDurablePrompt, writeManifest } from './tools/manifest.js'

// `node dist/export-tools.js <file>`: writes the tool manifest (tools/manifest.ts).
// `pnpm build` writes dist/tools.json with it and dist/durable-prompt.txt beside it; `pnpm gen:tools <file>` anywhere.

const out = process.argv[2]
if (!out) {
  console.error('usage: export-tools <file>')
  process.exit(2)
}
await writeManifest(out)
await writeDurablePrompt(path.join(path.dirname(out), 'durable-prompt.txt'))
