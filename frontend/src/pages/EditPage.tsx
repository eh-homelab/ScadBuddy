import { Link, Navigate, useLocation, useParams } from 'react-router'
import { api } from '../api/client'
import type { EditNavigationState } from '../lib/deeplink'
import { Spinner } from '../components/ui/Spinner'
import { useAsync } from '../lib/useAsync'

/**
 * `/edit/{output_id}` — the stable link stamped into every 3MF and attached to the
 * file Bambuddy holds. It resolves the output to its model and hands over to the
 * customizer, which loads the values from the same route.
 */
export function EditPage() {
  const { outputId = '' } = useParams()
  // A caller that already holds the output — the history list — hands it over rather
  // than making this route resolve what it just rendered. A pasted link carries no
  // state, so the fetch is still the general case.
  const handedOver = useLocation().state as EditNavigationState | null
  const preloaded = handedOver?.editTarget?.output_id === outputId ? handedOver.editTarget : null
  const fetched = useAsync(
    async () => (preloaded ? null : await api.getEditTarget(outputId)),
    [outputId, preloaded !== null],
  )
  // useAsync starts loading whether or not it has anything to fetch, so a handed-over
  // target would still flash the spinner for a tick before the redirect.
  const target = preloaded
    ? { loading: false, error: undefined, data: preloaded }
    : { loading: fetched.loading, error: fetched.error, data: fetched.data }

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

  // The resolved target rides along in router state: the customizer needs the same
  // payload, and without this every Edit click resolves the deep link twice — which
  // in the record-is-gone case means unzipping and re-parsing the 3MF twice.
  return (
    <Navigate
      to={`/m/${target.data.slug}?from=${outputId}`}
      state={{ editTarget: target.data } satisfies EditNavigationState}
      replace
    />
  )
}
