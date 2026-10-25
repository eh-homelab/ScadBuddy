import type { ReactNode } from 'react'
import { Link, useParams } from 'react-router'
import { ChevronDownIcon } from 'lucide-react'
import { api } from '../api/client'
import { NowPrinting } from '../components/prints/NowPrinting'
import { PrintQueue } from '../components/prints/PrintQueue'
import { PrintHistory } from '../components/prints/PrintHistory'
import { modelPath } from '../lib/deeplink'
import { nativeBackends } from '../lib/printerRegistry'
import { useAsync } from '../lib/useAsync'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'

/**
 * #4629 — a panel that reads Bambuddy only once opened: its content mounts, and so asks,
 * when the trigger opens it (Radix unmounts closed content).
 */
function OnOpen({ title, testId, children }: { title: string; testId: string; children: ReactNode }) {
  return (
    <Collapsible className="group/panel mb-6" data-testid={testId}>
      <CollapsibleTrigger className="flex w-full cursor-pointer items-center gap-2 rounded-md py-1 text-left text-sm font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
        <span className="flex-1">{title}</span>
        <ChevronDownIcon
          aria-hidden="true"
          className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]/panel:rotate-180"
        />
      </CollapsibleTrigger>
      <CollapsibleContent className="pt-2">{children}</CollapsibleContent>
    </Collapsible>
  )
}

/**
 * #310 — every print of every template (`/prints`, plan §2.7). On the hot path's backends
 * (#3818 tier A, `nativeBackends`) the queue and the archive history are Bambuddy's, so
 * they are read only once opened (#4629): opening the page asks Bambuddy nothing, and the
 * printer cards are the LAN reports' (`get_printers_now`). Until Settings answer neither
 * is shown, so a native page never asks first; a failed Settings read shows both, as before.
 */
export function PrintsPage() {
  const settings = useAsync(() => api.getSettings(), [])
  const settled = settings.data !== undefined || settings.error !== undefined
  const lazy = nativeBackends(settings.data)
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5">
          <h1 className="text-lg font-semibold tracking-tight">Print history</h1>
          <p className="mt-0.5 text-[13px] text-subtle">
            Every print of a ScadBuddy output, as Bambuddy archived it.
          </p>
        </div>
        <NowPrinting />
        {settled &&
          (lazy ? (
            <>
              <OnOpen title="Bambuddy's print queue" testId="print-queue-panel">
                <PrintQueue />
              </OnOpen>
              <OnOpen title="Bambuddy's print history" testId="print-history-panel">
                <PrintHistory />
              </OnOpen>
            </>
          ) : (
            <>
              <PrintQueue />
              <PrintHistory />
            </>
          ))}
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
          <Link to="/" className="text-[12px] text-subtle hover:text-ink">
            Models
          </Link>
          <span className="text-faint">/</span>
          <Link to={modelPath(slug)} className="text-[12px] text-subtle hover:text-ink">
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
