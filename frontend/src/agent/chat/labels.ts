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
 * #1288 — who an agent-made revision ran for, from its `ScadBuddy-Agent-Principal`
 * trailer (backend `core/authorship.py`): the principal ids of agent
 * `src/auth/principal.ts`, `browser`, `token:<id>`, `oidc:<issuer>#<sub>` and
 * `anonymous:<mcp session>`. An id it does not recognise is shown as it is.
 */
export function principalLabel(principal: string | null | undefined): string {
  if (!principal) return 'an unknown caller'
  if (principal === 'browser') return 'you'
  const colon = principal.indexOf(':')
  const kind = colon < 0 ? principal : principal.slice(0, colon)
  const rest = colon < 0 ? '' : principal.slice(colon + 1)
  switch (kind) {
    case 'token':
      return rest ? `MCP token ${rest}` : 'an MCP token'
    case 'oidc': {
      const subject = rest.slice(rest.lastIndexOf('#') + 1)
      return subject ? `OIDC user ${subject}` : 'an OIDC user'
    }
    case 'anonymous':
      return 'an anonymous MCP client'
    case 'flow':
      return 'a flow'
  }
  return principal
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
