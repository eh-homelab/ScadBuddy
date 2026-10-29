export const TOAST_MS = 6_000

/**
 * A button on a toast (#4813: an approval's Review and Deny), or a link when it has `href`;
 * clicking it dismisses the toast.
 */
export type ToastAction = { label: string; onClick?: () => void; href?: string; destructive?: boolean; userOnly?: boolean }

export type Toast = { id: number; title: string; href?: string; detail?: string; actions?: readonly ToastAction[]; ms: number }

let toasts: readonly Toast[] = []

export const getToasts = (): readonly Toast[] => toasts
let nextId = 1
const listeners = new Set<() => void>()
const timers = new Map<number, ReturnType<typeof setTimeout>>()

function publish(next: readonly Toast[]): void {
  toasts = next
  for (const listener of listeners) listener()
}

const holds = new Map<number, Set<'hover' | 'focus'>>()

/** Holds a toast's timer while it is hovered or focused (WCAG 2.2.1); {@link releaseToast} restarts it once neither is. */
export function holdToast(id: number, by: 'hover' | 'focus'): void {
  holds.set(id, (holds.get(id) ?? new Set()).add(by))
  clearTimeout(timers.get(id))
  timers.delete(id)
}

export function releaseToast(id: number, by: 'hover' | 'focus'): void {
  const held = holds.get(id)
  held?.delete(by)
  if (held && held.size > 0) return
  const shown = toasts.find((one) => one.id === id)
  if (!shown) return
  clearTimeout(timers.get(id))
  timers.set(id, setTimeout(() => dismissToast(id), shown.ms))
}

export function dismissToast(id: number): void {
  clearTimeout(timers.get(id))
  timers.delete(id)
  holds.delete(id)
  publish(toasts.filter((one) => one.id !== id))
}

/**
 * Shows a toast bottom-right for `ms` ({@link TOAST_MS} by default); a `<Toasts />` host must be
 * mounted. Returns its id, for {@link dismissToast}.
 */
export function toast({
  title,
  href,
  detail,
  actions,
  ms = TOAST_MS,
}: {
  title: string
  href?: string
  detail?: string
  actions?: readonly ToastAction[]
  ms?: number
}): number {
  const id = nextId++
  publish([...toasts, { id, title, href, detail, actions, ms }])
  timers.set(id, setTimeout(() => dismissToast(id), ms))
  return id
}

export function subscribeToasts(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** Empties the stack; for tests, which share the module. */
export function clearToasts(): void {
  for (const timer of timers.values()) clearTimeout(timer)
  timers.clear()
  holds.clear()
  publish([])
}
