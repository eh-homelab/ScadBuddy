import { readFile } from 'node:fs/promises'
import { z } from 'zod'
import { defineTool, text, ToolError, type Tool } from './registry.js'

// ScadBuddy's authoring conventions as a document an agent reads before it
// writes a template (#252, "Agent reference doc": customizer comment syntax,
// colour and font handling, and the verified OpenSCAD facts from CLAUDE.md).
// It is the plugin's `authoring` skill, the one text both Claude Code (through
// the plugin, #299) and ScadBuddy's own agent read, so the two cannot drift.
// Served as `scadbuddy://docs/authoring` (resources/catalog.ts) and as this
// tool.
//
// Where it is read from: `pnpm build` copies the skill to dist/docs/authoring.md
// (agent/package.json `build`; the Dockerfile's agent-build stage copies the
// skill in for it). Run from the source tree (tests, `pnpm dev`) there is no
// dist copy beside the module, so the skill is read where it lives.

const CANDIDATES = [
  new URL('../docs/authoring.md', import.meta.url),
  new URL('../../../plugins/scadbuddy/skills/authoring/SKILL.md', import.meta.url),
]

/** The skill without its YAML frontmatter (https://code.claude.com/docs/en/skills). */
export function stripFrontmatter(markdown: string): string {
  const match = /^---\r?\n[\s\S]*?\r?\n---\r?\n/.exec(markdown)
  return match ? markdown.slice(match[0].length).trimStart() : markdown
}

let cached: Promise<string> | undefined

export function authoringGuide(): Promise<string> {
  cached ??= (async () => {
    for (const candidate of CANDIDATES) {
      try {
        return stripFrontmatter(await readFile(candidate, 'utf8'))
      } catch (err) {
        if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err
      }
    }
    throw new ToolError('the authoring guide is not installed with this agent')
  })().catch((err: unknown) => {
    cached = undefined
    throw err
  })
  return cached
}

export const guideTools: Tool[] = [
  defineTool({
    name: 'get_authoring_guide',
    description:
      "ScadBuddy's authoring conventions, as Markdown: what a template is on disk, customizer comment " +
      'syntax, colours (one `// color` parameter per extruder, every solid in a `color()`), fonts (installed ' +
      'families only: "Lobster Two", never "Lobster"), open preview parts, verify.sh, and editing through ' +
      "these tools. Read it before writing or changing a template. Also the resource scadbuddy://docs/authoring.",
    input: z.object({}),
    risk: 'read',
    source: "ScadBuddy's authoring guide, shipped with the agent (plugins/scadbuddy/skills/authoring/SKILL.md)",
    routes: [],
    handler: async () => text(await authoringGuide()),
  }),
]
