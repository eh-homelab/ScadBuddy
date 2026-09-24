import { Link, Navigate, useParams } from 'react-router'
import { api } from '../api/client'
import { Spinner } from '../components/ui/Spinner'
import { useAsync } from '../lib/useAsync'

/**
 * `/edit/{output_id}` — the stable link stamped into every 3MF and attached to the
 * file Bambuddy holds. It resolves the output to its model and hands over to the
 * customizer, which loads the values from the same route.
 */
export function EditPage() {
  const { outputId = '' } = useParams()
  const target = useAsync(() => api.getEditTarget(outputId), [outputId])

  if (target.loading) {
    return (
      <p className="flex h-full items-center justify-center gap-2 text-[13px] text-muted">
        <Spinner /> Opening that output
      </p>
    )
  }

  if (target.error || !target.data) {
    return (
      <div role="alert" className="mx-auto max-w-lg px-4 py-16 text-center">
        <h1 className="text-[15px] font-medium">That output is gone</h1>
        <p className="mt-2 text-[13px] text-muted">
          Nothing in ScadBuddy has the id <span className="sb-num">{outputId}</span> any more, and
          no 3MF was left to read its parameters from.
        </p>
        <Link to="/" className="mt-4 inline-block text-[13px] text-accent underline">
          Back to models
        </Link>
      </div>
    )
  }

  return <Navigate to={`/m/${target.data.slug}?from=${outputId}`} replace />
}
