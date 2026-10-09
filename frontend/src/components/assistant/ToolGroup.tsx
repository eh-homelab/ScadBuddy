import { useId, useState } from 'react'
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

// #782 — tool calls as the panel shows them (design 2026-10-08 §3.3): each call is one
// line, its title and status, with its arguments and raw result behind Details; calls
// in a row form one group that opens and closes. Approval and question cards are never
// inside a group (chat/toolGroups.ts `feedBlocks`).

const STATUS_STYLE: Record<ToolStatus, string> = {
  running: 'text-muted',
  waiting_approval: 'text-warn',
  waiting_input: 'text-accent',
  done: 'text-ok',
  failed: 'text-warn',
  not_run: 'text-faint',
  stopped: 'text-faint',
}

/** A shape per status, so it does not rest on colour alone; the words beside it say it too. */
function StatusIcon({ status }: { status: ToolStatus }) {
  if (status === 'running') {
    return (
      <span
        aria-hidden="true"
        className="inline-block h-3 w-3 shrink-0 rounded-full border-2 border-line-strong border-t-accent motion-safe:animate-spin"
      />
    )
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
          <a key={image.name} href={url} target="_blank" rel="noreferrer noopener" className="block" title="Open the full image">
            <img
              src={url}
              alt={images.length > 1 ? `${title} (${i + 1} of ${images.length})` : title}
              loading="lazy"
              data-testid="agent-tool-image"
              className={
                size === 'full'
                  ? 'max-h-56 max-w-full rounded-[4px] border border-line bg-bg object-contain'
                  : 'h-10 w-10 rounded-[3px] border border-line bg-bg object-cover'
              }
            />
          </a>
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
  const detailsId = useId()
  const title = toolTitle(call)
  const { result } = call
  return (
    <div
      className={`rounded-[6px] border border-line bg-surface-2 px-2.5 py-1.5 text-[12.5px] ${nested ? 'ml-4' : ''}`}
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
        <button
          type="button"
          className="shrink-0 rounded-[4px] px-1 text-[11px] text-muted underline hover:text-ink"
          aria-expanded={open}
          aria-controls={detailsId}
          aria-label={`Details: ${title}`}
          onClick={() => setToggled(!open)}
        >
          Details
        </button>
      </div>
      {result && !result.ok && <p className="mt-1 text-warn">{result.summary}</p>}
      {result?.images && result.images.length > 0 && (
        <div className="mt-1.5">
          <ToolImages images={result.images} sessionId={sessionId} title={title} size="full" />
        </div>
      )}
      {result?.version && (
        <Link
          to={`/m/${encodeURIComponent(result.version.slug)}/versions`}
          className="mt-1 inline-block text-[11.5px] text-accent underline"
        >
          Undo from version {result.version.revision.slice(0, 7)}
        </Link>
      )}
      {result && result.sources.length > 0 && (
        <details open={advanced} className="mt-1">
          <summary className="cursor-pointer text-[11.5px] text-muted">
            Why? {result.sources.length} source{result.sources.length === 1 ? '' : 's'}
          </summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[11.5px] text-muted">
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
                  {source.ref && <span className="ml-1 font-mono text-faint">{source.ref}</span>}
                </li>
              )
            })}
          </ul>
        </details>
      )}
      <div id={detailsId} hidden={!open} className="mt-1.5 space-y-2" data-testid="agent-tool-details">
        {open && (
          <>
            <p className="font-mono text-[11px] text-faint">{call.name}</p>
            <JsonView label="Arguments" value={call.input} testId="agent-tool-arguments" />
            {result && <JsonView label="Result" value={result.summary} testId="agent-tool-result" />}
          </>
        )}
      </div>
    </div>
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
  const listId = useId()
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
  if (calls.length === 1) return rows[0]!
  const open = advanced || (toggled ?? isLiveStatus(status))
  const last = calls.at(-1)!
  const images = calls.flatMap((c) => c.result?.images ?? [])
  return (
    <section className="rounded-[6px] border border-line text-[12.5px]" data-testid="agent-tool-group" aria-label={`${calls.length} steps`}>
      <button
        type="button"
        className="flex w-full items-center gap-2 rounded-[6px] px-2.5 py-1.5 text-left hover:bg-surface-2"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setToggled(!open)}
      >
        <span aria-hidden="true" className="w-3 shrink-0 text-[10px] text-muted">
          {open ? '▾' : '▸'}
        </span>
        <StatusIcon status={status} />
        <span className="shrink-0 font-medium">{calls.length} steps</span>
        <span className="min-w-0 flex-1 truncate text-muted">{toolTitle(last)}</span>
        <span className={`shrink-0 text-[11px] ${STATUS_STYLE[status]}`} data-testid="agent-tool-group-status">
          {toolStatusLabel(status)}
        </span>
      </button>
      {!open && images.length > 0 && (
        <div className="px-2.5 pb-1.5">
          <ToolImages images={images} sessionId={sessionId} title={toolTitle(last)} size="thumb" />
        </div>
      )}
      <div id={listId} hidden={!open} className="space-y-1.5 px-1.5 pb-1.5">
        {open && rows}
      </div>
    </section>
  )
}
