import { HttpResponse, http } from 'msw'
import type { PackageFile, PackageFileContent } from '../../api/aiPlugins'

/**
 * A plugin package's files for review (#1029): `GET /api/v1/ai/plugin-packages/:name/files`
 * and `…/:name/file?path=` (agent/src/routes/pluginPackages.ts, files.ts). The packages
 * are `mocks/aiPlugins.ts`'s: `greeter` (its pin, and with `pending=true` its `v2` re-pin),
 * `shell` and the built-in `scadbuddy`. Greeter also has a binary `assets/icon.png` and a
 * `CHANGELOG.txt` longer than the preview, which is cut unless `full=true`.
 */

const base = '/api/v1/ai/plugin-packages'
export const PREVIEW_CHARS = 64 * 1024
export const LONG_CHANGELOG = Array.from({ length: 4000 }, (_, i) => `- change ${i + 1}: tidy up`).join('\n') + '\n'

export const GREETER_SKILL = [
  '---',
  'name: hello',
  'description: Greets the user by name.',
  'allowed-tools: mcp__scadbuddy__list_models',
  '---',
  '',
  '# Hello',
  '',
  'Say **hello** to the user, then list the models.',
  '',
].join('\n')

const GREETER_V1: Record<string, string | Uint8Array> = {
  '.claude-plugin/plugin.json': JSON.stringify({ name: 'greeter', version: '1.0.0', description: 'Says hello.' }, null, 2),
  '.mcp.json': JSON.stringify({ mcpServers: { mem: { type: 'http', url: 'https://mcp.example/mcp/' } } }, null, 2),
  'README.md': '# greeter\n\nA fixture.\n',
  'agents/helper.md': '---\nname: helper\ndescription: Helps.\n---\n\nHelp.\n',
  'commands/wave.md': '---\ndescription: Waves.\n---\n\nWave at the user.\n',
  'hooks/hooks.json': JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: 'prompt', prompt: 'Greeted?' }] }] } }, null, 2),
  'skills/hello/SKILL.md': GREETER_SKILL,
  'assets/icon.png': new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0]),
  'CHANGELOG.txt': LONG_CHANGELOG,
}
const GREETER_V2: Record<string, string | Uint8Array> = {
  ...Object.fromEntries(Object.entries(GREETER_V1).filter(([p]) => p !== 'README.md')),
  'skills/bye/SKILL.md': '---\nname: bye\ndescription: Says goodbye.\n---\n\nSay goodbye.\n',
}
const FILES: Record<string, Record<string, string | Uint8Array>> = {
  greeter: GREETER_V1,
  shell: {
    '.claude-plugin/plugin.json': JSON.stringify({ name: 'shell' }, null, 2),
    'hooks/hooks.json': JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: 'command', command: 'id' }] }] } }, null, 2),
    'skills/status/SKILL.md': '---\nname: status\n---\n\n!`git status`\n',
  },
  scadbuddy: {
    '.claude-plugin/plugin.json': JSON.stringify({ name: 'scadbuddy', version: '0.1.10' }, null, 2),
    'skills/authoring/SKILL.md': '---\nname: authoring\ndescription: Writes OpenSCAD templates.\n---\n\n# Authoring\n',
    'agents/model-author.md': '---\nname: model-author\n---\n\nAuthor models.\n',
  },
}

function filesOf(name: string, pending: boolean): Record<string, string | Uint8Array> | undefined {
  if (pending) return name === 'greeter' ? GREETER_V2 : undefined
  return FILES[name]
}

const encoder = new TextEncoder()
const sizeOf = (body: string | Uint8Array) => (typeof body === 'string' ? encoder.encode(body).length : body.length)

function mediaType(path: string, binary: boolean): string {
  if (path.endsWith('.md')) return 'text/markdown'
  if (path.endsWith('.json')) return 'application/json'
  if (path.endsWith('.png')) return 'image/png'
  return binary ? 'application/octet-stream' : 'text/plain'
}

export const handlers = [
  http.get(`${base}/:name/files`, ({ params, request }) => {
    const files = filesOf(String(params.name), new URL(request.url).searchParams.get('pending') === 'true')
    if (!files) return HttpResponse.json({ detail: `no plugin package named "${String(params.name)}"` }, { status: 404 })
    const list: PackageFile[] = Object.entries(files)
      .map(([path, body]) => ({ path, size: sizeOf(body) }))
      .sort((a, b) => (a.path < b.path ? -1 : 1))
    return HttpResponse.json({ files: list })
  }),
  http.get(`${base}/:name/file`, ({ params, request }) => {
    const query = new URL(request.url).searchParams
    const path = query.get('path') ?? ''
    const body = filesOf(String(params.name), query.get('pending') === 'true')?.[path]
    if (body === undefined) return HttpResponse.json({ detail: `no file "${path}"` }, { status: 404 })
    const binary = typeof body !== 'string'
    const truncated = !binary && query.get('full') !== 'true' && body.length > PREVIEW_CHARS
    const answer: PackageFileContent = {
      path,
      size: sizeOf(body),
      binary,
      media_type: mediaType(path, binary),
      truncated,
      content: binary ? null : truncated ? body.slice(0, PREVIEW_CHARS) : body,
    }
    return HttpResponse.json(answer)
  }),
]
