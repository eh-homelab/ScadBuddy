import { useState } from 'react'
import { ChevronRightIcon, Loader2Icon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Link } from 'react-router'
import { toolTitle } from '../../agent/chat/labels'
import { blobUrl, type ToolImage } from '../../agent/chat/protocol'
import {
  groupStatus,
  isLiveStatus,
  type ToolCall,
  type ToolStatus,
  toolStatusLabel,
} from '../../agent/chat/toolGroups'
import { safeHttpUrl } from '../../lib/safeUrl'
import { RiskBadge } from './badges'
import { JsonView } from './JsonView'
import { Tip } from './Tip'

// #782 — tool calls as the panel shows them (design 2026-10-08 §3.3): each call is one
// line, its title and status, with its arguments and raw result behind Details; calls
// in a row form one group that opens and closes. Approval and question cards are never
// inside a group (chat/toolGroups.ts `feedBlocks`).

const STATUS_STYLE: Record<ToolStatus, string> = {
  running: 'text-muted-foreground',
  waiting_approval: 'text-warn',
  waiting_input: 'text-primary',
  done: 'text-ok',
  failed: 'text-warn',
  not_run: 'text-muted-foreground',
  stopped: 'text-muted-foreground',
}

/** A shape per status, so it does not rest on colour alone; the words beside it say it too. */
function StatusIcon({ status }: { status: ToolStatus }) {
  if (status === 'running') {
    return <Loader2Icon aria-hidden="true" className="size-3 shrink-0 text-primary motion-safe:animate-spin" />
  }
  const glyph: Record<Exclude<ToolStatus, 'running'>, string> = {
    waiting_approval: '!',
    waiting_input: '?',
    done: '✓',
    failed: '✕',
    not_run: '–',
    stopped: '■',
  }
  return (
    <span aria-hidden="true" className={`inline-block w-3 shrink-0 text-center text-[12px] leading-none ${STATUS_STYLE[status]}`}>
      {glyph[status]}
    </span>
  )
}

function ToolImages({ images, sessionId, title, size }: { images: ToolImage[]; sessionId: string; title: string; size: 'full' | 'thumb' }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {images.map((image, i) => {
        const url = blobUrl(sessionId, image.name)
        return (
          <Tip key={image.name} content="Open the full image">
          <a href={url} target="_blank" rel="noreferrer noopener" className="block rounded-md outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
            <img
              src={url}
              alt={images.length > 1 ? `${title} (${i + 1} of ${images.length})` : title}
              loading="lazy"
              data-testid="agent-tool-image"
              className={
                size === 'full'
                  ? 'max-h-56 max-w-full rounded-md border border-border bg-background object-contain'
                  : 'h-10 w-10 rounded-sm border border-border bg-background object-cover'
              }
            />
          </a>
          </Tip>
        )
      })}
    </div>
  )
}

export function ToolCallView({
  call,
  status,
  sessionId,
  advanced,
  nested = false,
}: {
  call: ToolCall
  status: ToolStatus
  sessionId: string
  advanced: boolean
  /** A subagent's call shown under its `Agent` call. */
  nested?: boolean
}) {
  const [toggled, setToggled] = useState<boolean | undefined>(undefined)
  const open = toggled ?? advanced
  const [sourcesToggled, setSourcesToggled] = useState<boolean | undefined>(undefined)
  const title = toolTitle(call)
  const { result } = call
  return (
    <Collapsible
      open={open}
      onOpenChange={setToggled}
      className={`rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12.5px] ${nested ? 'ml-4' : ''}`}
      data-testid="agent-tool"
      data-status={status}
    >
      <div className="flex items-center gap-2">
        <StatusIcon status={status} />
        <span className="min-w-0 flex-1 break-words">{title}</span>
        <RiskBadge risk={call.risk} />
        <span className={`shrink-0 text-[11px] ${STATUS_STYLE[status]}`} data-testid="agent-tool-status">
          {toolStatusLabel(status)}
        </span>
        <CollapsibleTrigger asChild>
          <Button variant="link" size="xs" className="h-auto shrink-0 px-1 text-[11px] text-muted-foreground" aria-label={`Details: ${title}`}>
            Details
          </Button>
        </CollapsibleTrigger>
      </div>
      {/* Only a call that ran and broke: a refused or stopped one's result is Claude Code's
          note to the model ("The user doesn't want to proceed…"), kept under Details (#2149). */}
      {result && !result.ok && status === 'failed' && <p className="mt-1 text-warn">{result.summary}</p>}
      {result?.images && result.images.length > 0 && (
        <div className="mt-1.5">
          <ToolImages images={result.images} sessionId={sessionId} title={title} size="full" />
        </div>
      )}
      {result?.version && (
        <Link
          to={`/m/${encodeURIComponent(result.version.slug)}/versions`}
          className="mt-1 inline-block text-[11.5px] text-primary underline underline-offset-2"
        >
          Undo from version {result.version.revision.slice(0, 7)}
        </Link>
      )}
      {result && result.sources.length > 0 && (
        <Collapsible open={sourcesToggled ?? advanced} onOpenChange={setSourcesToggled} className="mt-1">
          <CollapsibleTrigger className="flex items-center gap-1 rounded-sm text-[11.5px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
            <ChevronRightIcon
              aria-hidden="true"
              className={`size-3 transition-transform motion-reduce:transition-none ${(sourcesToggled ?? advanced) ? 'rotate-90' : ''}`}
            />
            Why? {result.sources.length} source{result.sources.length === 1 ? '' : 's'}
          </CollapsibleTrigger>
          {/* Kept in the page while closed, as the disclosure it replaced kept them. */}
          <CollapsibleContent forceMount hidden={!(sourcesToggled ?? advanced)}>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[11.5px] text-muted-foreground">
            {result.sources.map((source, i) => {
              const href = safeHttpUrl(source.url)
              return (
                <li key={i}>
                  {href ? (
                    <a href={href} target="_blank" rel="noreferrer noopener" className="underline">
                      {source.title}
                    </a>
                  ) : (
                    source.title
                  )}
                  {source.ref && <span className="ml-1 font-mono text-muted-foreground/80">{source.ref}</span>}
                </li>
              )
            })}
          </ul>
          </CollapsibleContent>
        </Collapsible>
      )}
      <CollapsibleContent forceMount hidden={!open} className="mt-1.5 space-y-2" data-testid="agent-tool-details">
        {open && (
          <>
            <p className="font-mono text-[11px] text-muted-foreground">{call.name}</p>
            <JsonView label="Arguments" value={call.input} testId="agent-tool-arguments" />
            {result && <JsonView label="Result" value={result.summary} testId="agent-tool-result" />}
          </>
        )}
      </CollapsibleContent>
    </Collapsible>
  )
}

/**
 * One run of consecutive calls. One call is shown alone; two or more get a header that
 * opens and closes them, open while any is live, closed once all have ended, unless the
 * user chose (or Advanced is on). The header shows the group's images too, so they are
 * seen with it closed.
 */
export function ToolGroup({
  calls,
  statuses,
  sessionId,
  advanced,
}: {
  calls: ToolCall[]
  /** Each call's status, in order (chat/toolGroups.ts `toolStatus`). */
  statuses: ToolStatus[]
  sessionId: string
  advanced: boolean
}) {
  const [toggled, setToggled] = useState<boolean | undefined>(undefined)
  const status = groupStatus(statuses)
  const ids = new Set(calls.map((c) => c.id))
  const rows = calls.map((call, i) => (
    <ToolCallView
      key={call.id}
      call={call}
      status={statuses[i] ?? 'running'}
      sessionId={sessionId}
      advanced={advanced}
      nested={call.parent !== undefined && ids.has(call.parent)}
    />
  ))
  // #2149: one wrapper whatever the count, so a call keeps its place in the tree (and its
  // open Details) when a second joins its group.
  const many = calls.length > 1
  const open = !many || advanced || (toggled ?? isLiveStatus(status))
  const last = calls.at(-1)!
  const images = calls.flatMap((c) => c.result?.images ?? [])
  return (
    <Collapsible asChild open={open} onOpenChange={setToggled}>
    <section
      className={many ? 'rounded-lg border border-border text-[12.5px]' : undefined}
      data-testid={many ? 'agent-tool-group' : undefined}
      aria-label={many ? `${calls.length} steps` : undefined}
    >
      {many && (
        <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left outline-none hover:bg-ui-muted focus-visible:ring-3 focus-visible:ring-ring/50">
          <ChevronRightIcon
            aria-hidden="true"
            className={`size-3 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none ${open ? 'rotate-90' : ''}`}
          />
          <StatusIcon status={status} />
          <span className="shrink-0 font-medium">{calls.length} steps</span>
          <span className="min-w-0 flex-1 truncate text-muted-foreground">{toolTitle(last)}</span>
          <span className={`shrink-0 text-[11px] ${STATUS_STYLE[status]}`} data-testid="agent-tool-group-status">
            {toolStatusLabel(status)}
          </span>
        </CollapsibleTrigger>
      )}
      {many && !open && images.length > 0 && (
        <div className="px-2.5 pb-1.5">
          <ToolImages images={images} sessionId={sessionId} title={toolTitle(last)} size="thumb" />
        </div>
      )}
      <CollapsibleContent forceMount hidden={!open} className={many ? 'space-y-1.5 px-1.5 pb-1.5' : undefined}>
        {open && rows}
      </CollapsibleContent>
    </section>
    </Collapsible>
  )
}
