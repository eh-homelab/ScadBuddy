import type { AgentDefinition } from '@anthropic-ai/claude-agent-sdk'
import { readdir, readFile } from 'node:fs/promises'
import path from 'node:path'
import { OWN_PLUGIN_DIR } from './ownPlugin.js'

// The plugin's subagent definitions (plugins/scadbuddy/agents/*.md) as the SDK's
// AgentDefinition, read as agent-durable's subagents.py `load_agents` reads them: `key:
// value` lines and `key:` lists of `  - item`. A Workflow agent's chat runs as one of
// them (background/workflowAgents.ts): Claude Code runs a turn's main thread as a
// definition passed in `agents` and named by `agent` (measured on 2.1.296; a plugin's own
// agent named that way is not taken), so both modes pass it that way.

export const OWN_AGENTS_DIR = path.join(OWN_PLUGIN_DIR, 'agents')

function frontmatter(text: string): { fields: Record<string, string | string[]>; body: string } {
  const lines = text.split('\n')
  if (lines[0]?.trim() !== '---') throw new Error('no frontmatter')
  const end = lines.findIndex((l, i) => i > 0 && l.trim() === '---')
  if (end < 0) throw new Error('frontmatter is not closed')
  const fields: Record<string, string | string[]> = {}
  let key: string | undefined
  for (const line of lines.slice(1, end)) {
    if (line.startsWith('  - ') && key !== undefined) {
      const list = fields[key]
      fields[key] = [...(Array.isArray(list) ? list : []), line.slice(4).trim()]
    } else if (line.includes(':') && !line.startsWith(' ')) {
      const at = line.indexOf(':')
      key = line.slice(0, at).trim()
      const value = line.slice(at + 1).trim()
      if (value) fields[key] = value
    } else if (line.trim()) {
      throw new Error(`unsupported frontmatter line: ${JSON.stringify(line)}`)
    }
  }
  return { fields, body: lines.slice(end + 1).join('\n').trim() }
}

const names = (v: string | string[] | undefined): string[] | undefined =>
  v === undefined ? undefined : (Array.isArray(v) ? v : v.split(',')).map((s) => s.trim()).filter(Boolean)

/** Every definition in `dir`, by its `name:`. */
export async function loadAgentDefinitions(dir: string = OWN_AGENTS_DIR): Promise<Record<string, AgentDefinition>> {
  const out: Record<string, AgentDefinition> = {}
  for (const file of (await readdir(dir).catch(() => [])).filter((f) => f.endsWith('.md')).sort()) {
    const { fields, body } = frontmatter(await readFile(path.join(dir, file), 'utf8'))
    const name = fields.name
    if (typeof name !== 'string') continue
    const tools = names(fields.tools)
    const disallowedTools = names(fields.disallowedTools)
    const skills = names(fields.skills)
    out[name] = {
      description: String(fields.description ?? ''),
      prompt: body,
      ...(tools ? { tools } : {}),
      ...(disallowedTools ? { disallowedTools } : {}),
      ...(skills ? { skills } : {}),
      ...(typeof fields.model === 'string' ? { model: fields.model } : {}),
      ...(typeof fields.maxTurns === 'string' ? { maxTurns: Number(fields.maxTurns) } : {}),
    }
  }
  return out
}
