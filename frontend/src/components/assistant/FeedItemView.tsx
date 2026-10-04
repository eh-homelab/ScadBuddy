import { useState } from 'react'
import { Link } from 'react-router'
import { toolLabel } from '../../agent/chat/labels'
import { Markdown } from '../../agent/chat/Markdown'
import { ANSWER_MAX, type Question as AskedQuestion } from '../../agent/chat/protocol'
import type { FeedItem } from '../../agent/chat/state'
import { safeHttpUrl } from '../../lib/safeUrl'
import { Button } from '../ui/Button'
import { RiskBadge } from './badges'

type Tool = Extract<FeedItem, { kind: 'tool' }>
type Approval = Extract<FeedItem, { kind: 'approval' }>
type Memory = Extract<FeedItem, { kind: 'memory' }>
type QuestionItem = Extract<FeedItem, { kind: 'question' }>

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

/** What the user picked for one question: option labels, and their own words when `other`. */
type Choice = { picked: string[]; other: boolean; text: string }

const NO_CHOICE: Choice = { picked: [], other: false, text: '' }

/** One question's answer as the agent reads it, whether or not it fits ANSWER_MAX; undefined while it has none. */
function rawAnswerOf(q: AskedQuestion, c: Choice): string | undefined {
  const own = c.other ? c.text.trim() : ''
  if (c.other && !own) return undefined
  if (!q.multiSelect) return c.other ? own : c.picked[0]
  const parts = q.options.map((o) => o.label).filter((l) => c.picked.includes(l))
  // Marked, so the agent can tell the user's words from the picked labels (no label has a comma).
  if (own) parts.push(`Other: ${own}`)
  return parts.length ? parts.join(', ') : undefined
}

/** The answer to send, or undefined while it has none or is longer than the agent takes. */
function answerOf(q: AskedQuestion, c: Choice): string | undefined {
  const answer = rawAnswerOf(q, c)
  return answer !== undefined && answer.length <= ANSWER_MAX ? answer : undefined
}

/** The question's draft: its first option preview, what "Edit…" starts from. */
function draftOf(q: AskedQuestion): string | undefined {
  return q.options.find((o) => o.preview !== undefined)?.preview
}

/**
 * The preview a question shows: the picked option's own (none when it has none, so
 * picking Cancel never shows the draft it declines); before any pick, or while the
 * user edits, the draft.
 */
function previewOf(q: AskedQuestion, c: Choice): string | undefined {
  if (c.other || c.picked.length === 0) return draftOf(q)
  const picked = q.options.filter((o) => c.picked.includes(o.label) && o.preview !== undefined)
  return picked.length === 1 ? picked[0]!.preview : picked.map((o) => o.preview).join('\n\n---\n\n') || undefined
}

/** #815 — what an attention request's timer does, as its card says it. */
function attentionTimer(a: NonNullable<QuestionItem['attention']>): string {
  const at = new Date(a.expiresAt)
  // A wait can last a day: past today, the day is named, or tomorrow's 09:05 would read as this morning's.
  const today = at.toDateString() === new Date().toDateString()
  const time = at.toLocaleString([], { ...(today ? {} : { weekday: 'short' }), hour: '2-digit', minute: '2-digit' })
  const when = Number.isNaN(at.getTime()) ? 'soon' : `by ${time}`
  switch (a.onTimeout) {
    case 'proceed':
      return `No reply ${when}: it carries on with work that needs no approval. A timeout never approves anything.`
    case 'stop':
      return `No reply ${when}: it stops.`
    case 'wait':
      return `It waits for you until ${when.replace(/^by /, '')}, then stops.`
  }
}

/**
 * #940 — the agent asks the user (AskUserQuestion, or a subagent's `ask_user`): pick an option, or several when
 * the question allows it, or answer in your own words. A question with a draft (an
 * option's `preview`) shows it as Markdown, and its own-words choice is "Edit…",
 * starting from that draft, so editing it returns the edited text.
 */
function QuestionCard({ item, onAnswer }: { item: QuestionItem; onAnswer: (answers: string[]) => void }) {
  const [choices, setChoices] = useState<Choice[]>(() => item.questions.map(() => NO_CHOICE))
  const headingId = `question-${item.id}`
  const answers = item.questions.map((q, i) => answerOf(q, choices[i] ?? NO_CHOICE))
  const complete = answers.every((a) => a !== undefined)
  const update = (i: number, next: (c: Choice) => Choice) =>
    setChoices((all) => all.map((c, j) => (j === i ? next(c) : c)))
  return (
    <section
      aria-labelledby={headingId}
      className="rounded-[6px] border border-accent/60 bg-accent/5 px-3 py-2.5 text-[13px]"
      data-testid={item.attention ? 'agent-attention' : 'agent-question'}
    >
      <h3 id={headingId} className="text-[12.5px] font-semibold">
        {item.attention
          ? `The assistant needs you: ${item.questions[0]?.header ?? ''}`
          : item.questions.length === 1
            ? 'A question for you'
            : 'Questions for you'}
      </h3>
      {item.attention && item.state === 'pending' && (
        <p className="mt-1 text-[12px] text-muted" data-testid="agent-attention-timer">
          {attentionTimer(item.attention)}
        </p>
      )}
      {item.state === 'pending' ? (
        <form
          className="mt-1.5 space-y-3"
          data-agent-user-only=""
          onSubmit={(e) => {
            e.preventDefault()
            if (complete) onAnswer(answers as string[])
          }}
        >
          {item.questions.map((q, i) => {
            const choice = choices[i] ?? NO_CHOICE
            const preview = previewOf(q, choice)
            const draft = draftOf(q)
            const name = `${item.id}-${i}`
            const ownWords = draft === undefined ? 'Other…' : 'Edit…'
            const kind = q.multiSelect ? 'checkbox' : 'radio'
            const pick = (label: string) =>
              update(i, (c) =>
                q.multiSelect
                  ? { ...c, picked: c.picked.includes(label) ? c.picked.filter((l) => l !== label) : [...c.picked, label] }
                  : { ...c, picked: [label], other: false },
              )
            return (
              <fieldset key={name} className="space-y-1.5">
                <legend className="text-[13px]">
                  {q.header && (
                    <span className="mr-1.5 rounded-[4px] bg-surface-3 px-1.5 py-0.5 text-[11px] text-muted">{q.header}</span>
                  )}
                  {q.question}
                </legend>
                {preview !== undefined && (
                  <div
                    className="max-h-72 overflow-y-auto rounded-[6px] border border-line bg-surface-2 px-2.5 py-2"
                    data-testid="agent-question-preview"
                  >
                    <Markdown text={preview} />
                  </div>
                )}
                {q.options.map((o) => (
                  <label key={o.label} className="flex items-start gap-2">
                    <input
                      type={kind}
                      name={name}
                      className="mt-1"
                      checked={choice.picked.includes(o.label)}
                      onChange={() => pick(o.label)}
                    />
                    <span>
                      {o.label}
                      {o.description && <span className="block text-[11.5px] text-muted">{o.description}</span>}
                    </span>
                  </label>
                ))}
                <label className="flex items-start gap-2">
                  <input
                    type={kind}
                    name={name}
                    className="mt-1"
                    checked={choice.other}
                    onChange={() =>
                      update(i, (c) => ({
                        picked: q.multiSelect ? c.picked : [],
                        other: q.multiSelect ? !c.other : true,
                        text: c.text || (draft ?? ''),
                      }))
                    }
                  />
                  <span>{ownWords}</span>
                </label>
                {choice.other && (
                  <textarea
                    aria-label="Your answer"
                    maxLength={ANSWER_MAX}
                    rows={draft === undefined ? 2 : 6}
                    className="w-full rounded-[6px] border border-line bg-bg px-2 py-1.5 text-[13px]"
                    value={choice.text}
                    onChange={(e) => update(i, (c) => ({ ...c, text: e.target.value }))}
                  />
                )}
                {(rawAnswerOf(q, choice)?.length ?? 0) > ANSWER_MAX && (
                  <p className="text-[12px] text-warn" role="alert">
                    This answer is longer than the assistant takes ({ANSWER_MAX.toLocaleString()} characters). Shorten it to send.
                  </p>
                )}
              </fieldset>
            )
          })}
          <Button type="submit" variant="primary" size="sm" disabled={!complete}>
            {item.attention ? 'Send reply' : 'Send answer'}
          </Button>
        </form>
      ) : (
        <div className="mt-1.5 space-y-1">
          {item.questions.map((q, i) => (
            <p key={i} className="text-[12.5px]">
              {q.question}
            </p>
          ))}
          <p className="text-[12px] text-muted" role="status">
            {item.state === 'sent'
              ? 'Sending your answer…'
              : item.state === 'queued'
                ? 'Not connected: your answer goes first when the assistant reconnects.'
              : item.state === 'answered'
                ? `Answered${item.by ? ` by ${item.by.label}` : ''}: ${(item.answers ?? []).join(' · ')}`
                : item.attention
                  ? `No reply: ${item.reason ?? 'the request was cancelled'}.`
                  : `Not answered: ${item.reason ?? 'the question was cancelled'}.`}
          </p>
        </div>
      )}
    </section>
  )
}

export function FeedItemView({
  item,
  onDecide,
  onAnswer,
  advanced = false,
}: {
  item: FeedItem
  onDecide: (approvalId: string, approve: boolean) => void
  /** #940 — the user's answer to a question: one per question, in order. */
  onAnswer: (questionId: string, answers: string[]) => void
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
    case 'question':
      return <QuestionCard item={item} onAnswer={(answers) => onAnswer(item.id, answers)} />
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
