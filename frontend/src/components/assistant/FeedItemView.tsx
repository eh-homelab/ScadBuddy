import { Link } from 'react-router'
import { toolLabel } from '../../agent/chat/labels'
import { Markdown } from '../../agent/chat/Markdown'
import type { FeedItem } from '../../agent/chat/state'
import { safeHttpUrl } from '../../lib/safeUrl'
import { Button } from '../ui/Button'
import { RiskBadge } from './badges'

type Tool = Extract<FeedItem, { kind: 'tool' }>
type Approval = Extract<FeedItem, { kind: 'approval' }>
type Memory = Extract<FeedItem, { kind: 'memory' }>

function memoryHeadline(item: Memory): string {
  if (item.action === 'recall') {
    if (item.outcome === 'timeout') return 'Memory recall timed out'
    if (item.outcome === 'error') return 'Memory recall failed'
    const n = item.count ?? 0
    return n === 0 ? 'No memories recalled' : `Recalled ${n} ${n === 1 ? 'memory' : 'memories'}`
  }
  if (item.outcome === 'timeout') return 'Saving to memory timed out'
  if (item.outcome === 'error') return 'Could not save to memory'
  return 'Saved to memory'
}

/**
 * #818: an automatic recall or retain, as a quiet line. Basic mode shows only that
 * line, and nothing for a retain that worked; Advanced shows the bank, what was sent and what came back, open. Memories are
 * untrusted text, so they render as plain text, never Markdown.
 */
function MemoryLine({ item, advanced }: { item: Memory; advanced: boolean }) {
  const failed = item.outcome !== 'ok'
  const memories = item.memories ?? []
  if (!advanced) {
    // A retain follows every turn, so a successful one is noise in Basic mode; a
    // failed one still says so.
    if (item.action === 'retain' && !failed) return null
    return (
      <p className={`text-[11.5px] ${failed ? 'text-warn' : 'text-faint'}`} data-testid="agent-memory">
        {memoryHeadline(item)}
        {failed && item.detail && <> · {item.detail}</>}
      </p>
    )
  }
  return (
    <details open className="text-[11.5px] text-faint" data-testid="agent-memory">
      <summary className={`cursor-pointer ${failed ? 'text-warn' : ''}`}>{memoryHeadline(item)}</summary>
      <div className="mt-0.5 space-y-1 pl-3 text-muted">
        <p>
          Bank <span className="font-mono">{item.bank}</span>
          {item.action === 'recall' && item.outcome === 'ok' && <> · {item.count ?? 0} found</>}
          {item.detail && <> · {item.detail}</>}
        </p>
        {item.input !== undefined && (
          <details open data-testid="agent-memory-input">
            <summary className="cursor-pointer">{item.action === 'recall' ? 'Query' : 'Saved'}</summary>
            <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap font-mono text-[11px]">{item.input}</pre>
          </details>
        )}
        {memories.length > 0 && (
          <details open data-testid="agent-memory-output">
            <summary className="cursor-pointer">Memories</summary>
            <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap font-mono text-[11px]">{memories.join('\n')}</pre>
          </details>
        )}
      </div>
    </details>
  )
}

/** Basic mode shows what ran, how it ended and its sources, collapsed; Advanced adds its arguments, and opens both. */
function ToolCard({ item, advanced }: { item: Tool; advanced: boolean }) {
  const { result } = item
  return (
    <div className="rounded-[6px] border border-line bg-surface-2 px-2.5 py-2 text-[12.5px]" data-testid="agent-tool">
      <div className="flex items-center gap-2">
        <RiskBadge risk={item.risk} />
        <span className="min-w-0 truncate font-mono text-[12px]">{toolLabel(item.name)}</span>
        <span className="ml-auto shrink-0 text-[11px] text-faint">
          {result ? (result.ok ? 'done' : 'failed') : 'running…'}
        </span>
      </div>
      {advanced && (
        <details open className="mt-1" data-testid="agent-tool-arguments">
          <summary className="cursor-pointer text-[11.5px] text-muted">Arguments</summary>
          <pre className="mt-1 overflow-x-auto font-mono text-[11px] text-muted">
            {JSON.stringify(item.input, null, 2)}
          </pre>
        </details>
      )}
      {result && (
        <div className="mt-1.5 space-y-1">
          <p className={result.ok ? 'text-ink' : 'text-warn'}>{result.summary}</p>
          {result.version && (
            <Link
              to={`/m/${encodeURIComponent(result.version.slug)}/versions`}
              className="text-[11.5px] text-accent underline"
            >
              Undo from version {result.version.revision.slice(0, 7)}
            </Link>
          )}
          {result.sources.length > 0 && (
            <details open={advanced}>
              <summary className="cursor-pointer text-[11.5px] text-muted">Why? {result.sources.length} source{result.sources.length === 1 ? '' : 's'}</summary>
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
        </div>
      )}
    </div>
  )
}

function ApprovalCard({
  item,
  onDecide,
}: {
  item: Approval
  onDecide: (approve: boolean) => void
}) {
  const headingId = `approval-${item.id}`
  return (
    <section
      aria-labelledby={headingId}
      className="rounded-[6px] border border-warn/60 bg-warn/5 px-3 py-2.5 text-[13px]"
      data-testid="agent-approval"
    >
      <div className="flex items-center gap-2">
        <RiskBadge risk="outward" />
        <h3 id={headingId} className="text-[12.5px] font-semibold">
          Needs your approval
        </h3>
      </div>
      <p className="mt-1.5">{item.summary}</p>
      {item.state === 'pending' ? (
        <div className="mt-2 flex gap-2">
          {/* Human-only: the browser bridge (#254) must never let an agent press these. */}
          <Button variant="primary" size="sm" data-agent-user-only="" onClick={() => onDecide(true)}>
            Approve
          </Button>
          <Button variant="danger" size="sm" data-agent-user-only="" onClick={() => onDecide(false)}>
            Deny
          </Button>
        </div>
      ) : (
        <p className="mt-1.5 text-[12px] text-muted" role="status">
          {item.state === 'sent'
            ? 'Sending your answer…'
            : item.state === 'queued'
              ? 'Not connected: your answer goes first when the assistant reconnects.'
            : item.state === 'approved'
              ? `Approved${item.by ? ` by ${item.by.label}` : ''}.`
              : `Denied${item.by ? ` by ${item.by.label}` : ''}.`}
        </p>
      )}
    </section>
  )
}

export function FeedItemView({
  item,
  onDecide,
  advanced = false,
}: {
  item: FeedItem
  onDecide: (approvalId: string, approve: boolean) => void
  /** The panel's Advanced switch: every detail, open. Off, only the basics. */
  advanced?: boolean
}) {
  switch (item.kind) {
    case 'user':
      return (
        <div className="ml-8 rounded-[6px] bg-surface-3 px-2.5 py-1.5 text-[13px]">
          {item.author.kind !== 'browser' && (
            <p className="mb-0.5 text-[11px] text-faint">{item.author.label}</p>
          )}
          <p className="whitespace-pre-wrap">{item.text}</p>
        </div>
      )
    case 'assistant':
      return (
        <div className="text-[13px] leading-relaxed" data-testid="agent-text">
          <Markdown text={item.text} />
          {!item.done && (
            <span
              aria-hidden="true"
              className="ml-0.5 inline-block h-3 w-1.5 bg-accent align-middle motion-safe:animate-pulse"
            />
          )}
        </div>
      )
    case 'tool':
      return <ToolCard item={item} advanced={advanced} />
    case 'approval':
      return <ApprovalCard item={item} onDecide={(approve) => onDecide(item.id, approve)} />
    case 'memory':
      return <MemoryLine item={item} advanced={advanced} />
    case 'error':
      return (
        <p role="alert" className="text-[12.5px] text-warn">
          {item.message}
        </p>
      )
  }
}
