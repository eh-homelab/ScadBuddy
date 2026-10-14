export const TOAST_MS = 6_000

export type Toast = { id: number; title: string; href?: string }

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
  if (!toasts.some((one) => one.id === id)) return
  clearTimeout(timers.get(id))
  timers.set(id, setTimeout(() => dismissToast(id), TOAST_MS))
}

export function dismissToast(id: number): void {
  clearTimeout(timers.get(id))
  timers.delete(id)
  holds.delete(id)
  publish(toasts.filter((one) => one.id !== id))
}

/** Shows a toast bottom-right for {@link TOAST_MS}; a `<Toasts />` host must be mounted. */
export function toast({ title, href }: { title: string; href?: string }): void {
  const id = nextId++
  publish([...toasts, { id, title, href }])
  timers.set(id, setTimeout(() => dismissToast(id), TOAST_MS))
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
