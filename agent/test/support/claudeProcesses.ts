import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'

export type ClaudeProcess = { pid: number; args: string }

type Stat = { ppid: number; state: string }

// /proc/<pid>/stat is `pid (comm) state ppid …`; comm may hold spaces and
// parentheses, so the fields are read after its last `)` (proc_pid_stat(5)).
function statOf(pid: number): Stat | undefined {
  try {
    const text = readFileSync(`/proc/${pid}/stat`, 'utf8')
    const [state, ppid] = text.slice(text.lastIndexOf(')') + 2).split(' ')
    return { state: state ?? '', ppid: Number(ppid) }
  } catch {
    return undefined
  }
}

function argsOf(pid: number): string[] {
  try {
    return readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0').filter(Boolean)
  } catch {
    return []
  }
}

/**
 * The bundled Claude Code processes (`…/claude-agent-sdk-<platform>-<arch>/claude`)
 * below `root` that are still running (#5030). Linux only: elsewhere there is no
 * /proc to read, and it returns none.
 */
export function liveClaudeCodes(root = process.pid): ClaudeProcess[] {
  let entries: string[]
  try {
    entries = readdirSync('/proc')
  } catch {
    return []
  }
  const children = new Map<number, number[]>()
  const states = new Map<number, string>()
  for (const entry of entries) {
    const pid = Number(entry)
    if (!Number.isInteger(pid)) continue
    const stat = statOf(pid)
    if (!stat) continue
    states.set(pid, stat.state)
    children.set(stat.ppid, [...(children.get(stat.ppid) ?? []), pid])
  }
  const found: ClaudeProcess[] = []
  const queue = [...(children.get(root) ?? [])]
  for (let pid = queue.shift(); pid !== undefined; pid = queue.shift()) {
    queue.push(...(children.get(pid) ?? []))
    if (states.get(pid) === 'Z') continue
    const args = argsOf(pid)
    const exe = args[0] ?? ''
    if (path.basename(exe) === 'claude' && path.basename(path.dirname(exe)).startsWith('claude-agent-sdk-')) {
      found.push({ pid, args: args.join(' ') })
    }
  }
  return found
}

/** Waits up to `ms` for every Claude Code below this process to exit; returns those still running. */
export async function waitForClaudeCodes(ms: number): Promise<ClaudeProcess[]> {
  const deadline = Date.now() + ms
  for (;;) {
    const left = liveClaudeCodes()
    if (left.length === 0 || Date.now() >= deadline) return left
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
}
