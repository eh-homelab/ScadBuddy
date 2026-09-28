import { Link, useParams } from 'react-router'
import { api } from '../api/client'
import { PrintHistory } from '../components/prints/PrintHistory'
import { PrintStatus } from '../components/prints/PrintStatus'
import { printLabel } from '../components/prints/prints'
import { Spinner } from '../components/ui/Spinner'
import { modelPath } from '../lib/deeplink'
import { useAsync } from '../lib/useAsync'

/** #310 — every print of every template (`/prints`, plan §2.7). */
export function PrintsPage() {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5">
          <h1 className="text-lg font-semibold tracking-tight">Print history</h1>
          <p className="mt-0.5 text-[13px] text-muted">
            Every print of a ScadBuddy output, as Bambuddy archived it.
          </p>
        </div>
        <PrintHistory />
      </div>
    </div>
  )
}

/** #310 — a template's Prints tab (`/m/:slug/prints`): the same list, for this template. */
export function TemplatePrintsPage() {
  const { slug = '' } = useParams()
  const model = useAsync(() => api.getModel(slug), [slug])
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5 flex items-baseline gap-2">
          <Link to="/" className="text-[12px] text-muted hover:text-ink">
            Models
          </Link>
          <span className="text-faint">/</span>
          <Link to={modelPath(slug)} className="text-[12px] text-muted hover:text-ink">
            {model.data?.name ?? slug}
          </Link>
          <span className="text-faint">/</span>
          <h1 className="text-[13px] font-medium">Prints</h1>
        </div>
        <PrintHistory key={slug} fixedSlug={slug} />
      </div>
    </div>
  )
}

/**
 * `/prints/:archiveId` until the print detail page (#311) replaces it: a history row
 * links here, and without a route the catch-all would bounce it back to the catalogue.
 * It names the print and leads back; everything else is #311's.
 */
export function PrintPlaceholderPage() {
  const { archiveId = '' } = useParams()
  const print = useAsync(() => api.getPrint(Number(archiveId)), [archiveId])
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5 flex items-baseline gap-2">
          <Link to="/prints" className="text-[12px] text-muted hover:text-ink">
            Print history
          </Link>
          <span className="text-faint">/</span>
          <span className="sb-num text-[12px] text-muted">#{archiveId}</span>
        </div>
        {print.loading && (
          <p className="flex items-center gap-2 py-16 text-[13px] text-muted">
            <Spinner /> Loading the print
          </p>
        )}
        {print.error && (
          <p role="alert" className="rounded-[6px] border border-warn/40 bg-warn/8 p-4 text-[13px] text-warn">
            {print.error.message}
          </p>
        )}
        {print.data && (
          <>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-semibold tracking-tight">{printLabel(print.data)}</h1>
              <PrintStatus status={print.data.status} />
            </div>
            <p className="mt-2 text-[13px] text-muted">
              <Link to={modelPath(print.data.slug)} className="underline underline-offset-2 hover:text-ink">
                Customize from this template
              </Link>
            </p>
          </>
        )}
      </div>
    </div>
  )
}
