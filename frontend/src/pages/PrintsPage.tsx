import { Link, useParams } from 'react-router'
import { api } from '../api/client'
import { PrintHistory } from '../components/prints/PrintHistory'
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
