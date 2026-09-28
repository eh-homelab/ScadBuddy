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
