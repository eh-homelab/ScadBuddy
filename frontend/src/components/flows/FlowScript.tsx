import { lazy, Suspense } from 'react'

const CodeView = lazy(async () => ({ default: (await import('../CodeView')).CodeView }))

/** #2350 — a flow version's script, read only, with Python highlighting loaded lazily. */
export function FlowScript({ script, label = 'Script' }: { script: string; label?: string }) {
  return (
    <Suspense fallback={<p className="text-[13px] text-muted">Loading the script…</p>}>
      <CodeView text={script} language="python" label={label} />
    </Suspense>
  )
}
