import { Link } from 'react-router'
import { toolLabel } from '../../agent/chat/labels'
import { Markdown } from '../../agent/chat/Markdown'
import type { FeedItem } from '../../agent/chat/state'
import { safeHttpUrl } from '../../lib/safeUrl'
import { Button } from '../ui/Button'
import { RiskBadge } from './badges'

type Tool = Extract<FeedItem, { kind: 'tool' }>
type Approval = Extract<FeedItem, { kind: 'approval' }>

function ToolCard({ item }: { item: Tool }) {
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
      <details className="mt-1">
        <summary className="cursor-pointer text-[11.5px] text-muted">Arguments</summary>
        <pre className="mt-1 overflow-x-auto font-mono text-[11px] text-muted">
          {JSON.stringify(item.input, null, 2)}
        </pre>
      </details>
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
            <details>
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
}: {
  item: FeedItem
  onDecide: (approvalId: string, approve: boolean) => void
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
      return <ToolCard item={item} />
    case 'approval':
      return <ApprovalCard item={item} onDecide={(approve) => onDecide(item.id, approve)} />
    case 'error':
      return (
        <p role="alert" className="text-[12.5px] text-warn">
          {item.message}
        </p>
      )
  }
}
