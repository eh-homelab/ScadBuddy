import { Link, Navigate, useLocation, useParams } from 'react-router'
import { api } from '../api/client'
import { arrangedSources } from '../lib/arrange'
import { modelPath, type EditNavigationState } from '../lib/deeplink'
import { PageLoading, PageProblem } from '../components/PageState'
import { Button } from '@/components/ui/button'
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

  if (target.loading) return <PageLoading label="Opening that output" rows={1} />

  if (target.error || !target.data) {
    return (
      <PageProblem
        title="That output is gone"
        message={
          <>
            Nothing in ScadBuddy has the id <span className="sb-num">{outputId}</span> any more, and no 3MF was
            left to read its parameters from.
          </>
        }
      >
        <Button asChild variant="outline">
          <Link to="/">Back to models</Link>
        </Button>
      </PageProblem>
    )
  }

  // spec 2026-09-27 §7: an arranged output lays out objects from several outputs, so
  // there is no one set of template inputs to reopen.
  const arrangedFrom = (target.data.arranged_from ?? []).length
  if (arrangedFrom > 0) {
    const sources = arrangedSources(target.data.arranged_from ?? [])
    return (
      <PageProblem
        alert={false}
        title={`${target.data.name ?? 'This output'} was arranged`}
        message={`It lays out objects from ${sources}, so it has no one set of parameters to open in the customizer.`}
      >
        <Button asChild variant="outline">
          <Link to={modelPath(target.data.slug, 'history')}>Open its history</Link>
        </Button>
      </PageProblem>
    )
  }

  // The resolved target rides along in router state: the customizer needs the same
  // payload, and without this every Edit click resolves the deep link twice — which
  // in the record-is-gone case means unzipping and re-parsing the 3MF twice.
  return (
    <Navigate
      to={`${modelPath(target.data.slug)}?from=${outputId}`}
      state={{ editTarget: target.data } satisfies EditNavigationState}
      replace
    />
  )
}
