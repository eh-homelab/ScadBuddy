import type { SessionStatus } from './protocol'

const STATUS_LABEL: Record<SessionStatus, string> = {
  running: 'Working',
  waiting_input: 'Waiting for input',
  waiting_approval: 'Waiting for approval',
  idle: 'Idle',
  done: 'Done',
  failed: 'Failed',
}

export function statusLabel(status: SessionStatus): string {
  return STATUS_LABEL[status]
}

/**
 * `mcp__scadbuddy__set_parameters` → `set_parameters`; a plugin's tool keeps its
 * server name (`hindsight: recall`). SDK MCP tools are named `mcp__<server>__<tool>`
 * (spec §5.1).
 */
export function toolLabel(name: string): string {
  const parts = name.split('__')
  if (parts[0] === 'mcp' && parts.length >= 3) {
    const tool = parts.slice(2).join('__')
    return parts[1] === 'scadbuddy' ? tool : `${parts[1]}: ${tool}`
  }
  return name
}

/** `render_model` → `Render model`. */
function words(name: string): string {
  const spaced = name.replace(/[_-]+/g, ' ').trim()
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

const text = (value: unknown): string | undefined =>
  typeof value === 'string' && value.trim() ? value.trim() : undefined

/**
 * #782 — what a call does, in words: the title the tool declared (agent
 * `src/tools/registry.ts` `ToolSpec.title`), else the panel's own for a tool outside
 * ScadBuddy's registry. Claude Code's built-ins name what they act on; a plugin's tool
 * is its name in words with its server (`mcp__hindsight__recall` → `Recall (hindsight)`).
 */
export function toolTitle(call: { name: string; input: Record<string, unknown>; title?: string }): string {
  if (call.title) return call.title
  const { name, input } = call
  switch (name) {
    case 'Agent':
    case 'Task': {
      const what = text(input.description) ?? text(input.subagent_type)
      return what ? `Subagent: ${what}` : 'Subagent'
    }
    case 'Skill': {
      const skill = text(input.skill) ?? text(input.command)
      return skill ? `Skill: ${skill}` : 'Skill'
    }
    case 'AskUserQuestion':
      return 'Ask you'
  }
  const parts = name.split('__')
  if (parts[0] === 'mcp' && parts.length >= 3) {
    const tool = words(parts.slice(2).join('__'))
    const slug = text(input.slug)
    if (parts[1] === 'scadbuddy') return slug ? `${tool} → ${slug}` : tool
    return `${tool} (${parts[1]})`
  }
  return words(name)
}
