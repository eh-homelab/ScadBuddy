import { decodeText, WorkspaceError } from '../workspace/store.js'
import { type BuiltinResult, type FileToolContext, WORKSPACE_ROOT } from './files.js'

// Claude Code's task list (TaskCreate, TaskGet, TaskList, TaskUpdate), ReportFindings and
// the built-ins that answer the same in either mode (EnterWorktree, ExitWorktree,
// SendMessage) for a durable turn (#2393). A classic turn's Claude Code keeps its task
// list in its own process; a durable turn is many processes, so the list is a workspace
// file, TASKS_PATH. The wording is Claude Code 2.1.296's, measured in a classic turn.

/** The durable session's task list, a workspace file. */
export const TASKS_PATH = '_claude/tasks.json'

type Status = 'pending' | 'in_progress' | 'completed'
type Task = {
  id: string
  subject: string
  description: string
  activeForm?: string
  status: Status
  owner?: string
  blocks: string[]
  blockedBy: string[]
  metadata?: Record<string, unknown>
  /** The TaskCreate call that made it, by its tool_use id: a retry of that call answers this task (#3201). */
  call?: string
}
type TaskList = { next: number; tasks: Task[] }

const ok = (text: string): BuiltinResult => ({ text })

/** The model can write the list file with Write: one that is no task list fails the call, saying which file. */
const malformed = (): BuiltinResult => ({
  text: `The task list file ${WORKSPACE_ROOT}/${TASKS_PATH} is not a valid task list; fix it or delete it to start a new list.`,
  isError: true,
})

function isTaskList(value: unknown): value is TaskList {
  if (typeof value !== 'object' || value === null) return false
  const { next, tasks } = value as Partial<TaskList>
  return (
    Number.isSafeInteger(next) &&
    Array.isArray(tasks) &&
    tasks.every(
      (t: unknown) =>
        typeof t === 'object' &&
        t !== null &&
        typeof (t as Task).id === 'string' &&
        Array.isArray((t as Task).blocks) &&
        Array.isArray((t as Task).blockedBy),
    )
  )
}

async function loadTasks(ctx: FileToolContext): Promise<{ list: TaskList; sha: string | null } | undefined> {
  const file = await ctx.files.read(ctx.ws, TASKS_PATH)
  if (!file) return { list: { next: 1, tasks: [] }, sha: null }
  const text = decodeText(file.bytes, file.mediaType)
  let list: unknown
  try {
    list = JSON.parse(text ?? '')
  } catch {
    return undefined
  }
  return isTaskList(list) ? { list, sha: file.sha256 } : undefined
}

/** Runs `change` on the list and stores it, again from the top if another call wrote it meanwhile. */
async function withTasks(ctx: FileToolContext, change: (list: TaskList) => BuiltinResult): Promise<BuiltinResult> {
  for (let attempt = 0; ; attempt++) {
    const loaded = await loadTasks(ctx)
    if (!loaded) return malformed()
    const { list, sha } = loaded
    const result = change(list)
    try {
      await ctx.files.write(ctx.ws, TASKS_PATH, new TextEncoder().encode(JSON.stringify(list)), {
        mediaType: 'application/json',
        source: { tool: 'TaskList' },
        expectedSha256: sha,
      })
      return result
    } catch (err) {
      if (!(err instanceof WorkspaceError && err.code === 'conflict') || attempt >= 4) throw err
    }
  }
}

export function taskCreate(
  ctx: FileToolContext,
  input: { subject: string; description: string; activeForm?: string; metadata?: Record<string, unknown> },
  callId?: string,
): Promise<BuiltinResult> {
  return withTasks(ctx, (list) => {
    const made = callId === undefined ? undefined : list.tasks.find((t) => t.call === callId)
    if (made) return ok(`Task #${made.id} created successfully: ${made.subject}`)
    const id = String(list.next++)
    list.tasks.push({
      id,
      subject: input.subject,
      description: input.description,
      ...(input.activeForm !== undefined ? { activeForm: input.activeForm } : {}),
      status: 'pending',
      blocks: [],
      blockedBy: [],
      ...(input.metadata !== undefined ? { metadata: input.metadata } : {}),
      ...(callId !== undefined ? { call: callId } : {}),
    })
    return ok(`Task #${id} created successfully: ${input.subject}`)
  })
}

const live = (list: TaskList, ids: string[]) => ids.filter((id) => list.tasks.some((t) => t.id === id))

export async function taskGet(ctx: FileToolContext, input: { taskId: string }): Promise<BuiltinResult> {
  const loaded = await loadTasks(ctx)
  if (!loaded) return malformed()
  const { list } = loaded
  const task = list.tasks.find((t) => t.id === input.taskId)
  if (!task) return ok('Task not found')
  const lines = [`Task #${task.id}: ${task.subject}`, `Status: ${task.status}`, `Description: ${task.description}`]
  const blockedBy = live(list, task.blockedBy)
  const blocks = live(list, task.blocks)
  if (blockedBy.length) lines.push(`Blocked by: ${blockedBy.map((id) => `#${id}`).join(', ')}`)
  if (blocks.length) lines.push(`Blocks: ${blocks.map((id) => `#${id}`).join(', ')}`)
  if (task.owner) lines.push(`Owner: ${task.owner}`)
  return ok(lines.join('\n'))
}

export async function taskList(ctx: FileToolContext): Promise<BuiltinResult> {
  const loaded = await loadTasks(ctx)
  if (!loaded) return malformed()
  const { list } = loaded
  if (!list.tasks.length) return ok('No tasks found')
  return ok(
    list.tasks
      .map((t) => {
        const blockedBy = live(list, t.blockedBy)
        return `#${t.id} [${t.status}] ${t.subject}${t.owner ? ` (${t.owner})` : ''}${blockedBy.length ? ` [blocked by ${blockedBy.map((id) => `#${id}`).join(', ')}]` : ''}`
      })
      .join('\n'),
  )
}

export function taskUpdate(
  ctx: FileToolContext,
  input: {
    taskId: string
    subject?: string
    description?: string
    activeForm?: string
    status?: Status | 'deleted'
    addBlocks?: string[]
    addBlockedBy?: string[]
    owner?: string
    metadata?: Record<string, unknown>
  },
): Promise<BuiltinResult> {
  return withTasks(ctx, (list) => {
    const task = list.tasks.find((t) => t.id === input.taskId)
    if (!task) return ok('Task not found')
    if (input.status === 'deleted') {
      list.tasks = list.tasks.filter((t) => t !== task)
      return ok(`Updated task #${task.id} deleted`)
    }
    const changed: string[] = []
    const fields = ['subject', 'description', 'activeForm', 'status', 'owner'] as const
    for (const field of fields) {
      const value = input[field]
      if (value === undefined) continue
      Object.assign(task, { [field]: value })
      changed.push(field)
    }
    if (input.metadata !== undefined) {
      task.metadata = { ...task.metadata, ...input.metadata }
      changed.push('metadata')
    }
    if (input.addBlocks?.length) {
      for (const id of input.addBlocks) {
        if (!task.blocks.includes(id)) task.blocks.push(id)
        const other = list.tasks.find((t) => t.id === id)
        if (other && !other.blockedBy.includes(task.id)) other.blockedBy.push(task.id)
      }
      changed.push('blocks')
    }
    if (input.addBlockedBy?.length) {
      for (const id of input.addBlockedBy) {
        if (!task.blockedBy.includes(id)) task.blockedBy.push(id)
        const other = list.tasks.find((t) => t.id === id)
        if (other && !other.blocks.includes(task.id)) other.blocks.push(task.id)
      }
      changed.push('blockedBy')
    }
    return ok(changed.length ? `Updated task #${task.id} ${changed.join(', ')}` : `No changes to task #${task.id}`)
  })
}

export function reportFindings(input: { findings: unknown[] }): BuiltinResult {
  const n = input.findings.length
  return ok(n === 0 ? 'No findings reported.' : `${n} finding${n === 1 ? '' : 's'} reported.`)
}

// A classic turn's Claude Code runs in the session's directory, which is no git
// repository, and a durable turn's workspace is none either: both answer this way.
export function enterWorktree(): BuiltinResult {
  return {
    text:
      'Cannot create a worktree: not in a git repository and no WorktreeCreate hooks are configured. ' +
      'Configure WorktreeCreate/WorktreeRemove hooks in settings.json to use worktree isolation with other VCS systems.',
    isError: true,
  }
}

export function exitWorktree(): BuiltinResult {
  return { text: '<tool_use_error>No active EnterWorktree session to exit.</tool_use_error>', isError: true }
}

// A session runs no agent team, in either mode.
export function sendMessage(input: { to?: string }): BuiltinResult {
  return {
    text: JSON.stringify({
      success: false,
      message: `No agent named '${String(input.to ?? '')}' is reachable.\nCheck the spelling, or use the agent ID from a background agent's spawn result.`,
    }),
  }
}
