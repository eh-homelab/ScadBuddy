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

export function dismissToast(id: number): void {
  clearTimeout(timers.get(id))
  timers.delete(id)
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
  publish([])
}
