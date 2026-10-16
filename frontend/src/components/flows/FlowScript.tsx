import { lazy, Suspense } from 'react'
import { Skeleton } from '@/components/ui/skeleton'

const CodeView = lazy(async () => ({ default: (await import('../CodeView')).CodeView }))

/** #2350 — a flow version's script, read only, with Python highlighting loaded lazily. */
export function FlowScript({ script, label = 'Script' }: { script: string; label?: string }) {
  return (
    <Suspense fallback={<Skeleton role="status" aria-label="Loading the script" className="h-40 w-full rounded-xl" />}>
      <CodeView text={script} language="python" label={label} />
    </Suspense>
  )
}
