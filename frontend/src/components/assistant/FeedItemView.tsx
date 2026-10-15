import { useState, type ReactNode } from 'react'
import { ChevronRightIcon, GitForkIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Textarea } from '@/components/ui/textarea'
import { Markdown } from '../../agent/chat/Markdown'
import { ANSWER_MAX, type Question as AskedQuestion, type DoneAttention, isDone } from '../../agent/chat/protocol'
import type { FeedItem } from '../../agent/chat/state'
import { formatBytes } from '../../lib/format'
import { RiskBadge } from './badges'
import { SentImages } from './SentImages'
import { Tip } from './Tip'
import { ToolCallView } from './ToolGroup'

type Approval = Extract<FeedItem, { kind: 'approval' }>
type Memory = Extract<FeedItem, { kind: 'memory' }>
type QuestionItem = Extract<FeedItem, { kind: 'question' }>

/** #3673 — a fold that starts open, as the disclosure it replaced did: Advanced shows memory's details. */
function Fold({
  summary,
  className = '',
  summaryClassName = '',
  testId,
  children,
}: {
  summary: ReactNode
  className?: string
  summaryClassName?: string
  testId?: string
  children: ReactNode
}) {
  const [open, setOpen] = useState(true)
  return (
    <Collapsible open={open} onOpenChange={setOpen} className={className} data-testid={testId}>
      <CollapsibleTrigger
        className={`flex items-center gap-1 rounded-sm text-left outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 ${summaryClassName}`}
      >
        <ChevronRightIcon
          aria-hidden="true"
          className={`size-3 shrink-0 transition-transform motion-reduce:transition-none ${open ? 'rotate-90' : ''}`}
        />
        {summary}
      </CollapsibleTrigger>
      <CollapsibleContent>{children}</CollapsibleContent>
    </Collapsible>
  )
}

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
      <p className={`text-[11.5px] ${failed ? 'text-warn' : 'text-muted-foreground'}`} data-testid="agent-memory">
        {memoryHeadline(item)}
        {failed && item.detail && <> · {item.detail}</>}
      </p>
    )
  }
  return (
    <Fold
      summary={memoryHeadline(item)}
      className="text-[11.5px] text-muted-foreground"
      summaryClassName={failed ? 'text-warn' : ''}
      testId="agent-memory"
    >
      <div className="mt-0.5 space-y-1 pl-4 text-muted-foreground">
        <p>
          Bank <span className="font-mono">{item.bank}</span>
          {item.action === 'recall' && item.outcome === 'ok' && <> · {item.count ?? 0} found</>}
          {item.detail && <> · {item.detail}</>}
        </p>
        {item.input !== undefined && (
          <Fold summary={item.action === 'recall' ? 'Query' : 'Saved'} testId="agent-memory-input">
            <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-ui-muted/50 p-2 font-mono text-[11px] whitespace-pre-wrap">
              {item.input}
            </pre>
          </Fold>
        )}
        {memories.length > 0 && (
          <Fold summary="Memories" testId="agent-memory-output">
            <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-ui-muted/50 p-2 font-mono text-[11px] whitespace-pre-wrap">
              {memories.join('\n')}
            </pre>
          </Fold>
        )}
      </div>
    </Fold>
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
    <Card
      size="sm"
      role="region"
      aria-labelledby={headingId}
      className="gap-1.5 bg-destructive/5 px-3 text-[13px] ring-destructive/40"
      data-testid="agent-approval"
    >
      <div className="flex items-center gap-2">
        <RiskBadge risk="outward" />
        <h3 id={headingId} className="text-[13px] font-semibold">
          Needs your approval
        </h3>
      </div>
      <p>{item.summary}</p>
      {item.state === 'pending' ? (
        <div className="mt-0.5 flex gap-2">
          {/* Human-only: the browser bridge (#254) must never let an agent press these. */}
          <Button size="sm" data-agent-user-only="" onClick={() => onDecide(true)}>
            Approve
          </Button>
          <Button variant="destructive" size="sm" data-agent-user-only="" onClick={() => onDecide(false)}>
            Deny
          </Button>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground" role="status">
          {item.state === 'sent'
            ? 'Sending your answer…'
            : item.state === 'closed'
              ? `Your decision was not taken: ${item.reason ?? 'the request is no longer waiting'}.`
              : item.state === 'approved'
              ? `Approved${item.by ? ` by ${item.by.label}` : ''}.`
              : item.state === 'expired'
              ? 'Expired: nobody decided in time, so it was not run.'
              : item.state === 'cancelled'
              ? `Cancelled: ${item.reason ?? 'it is no longer waiting'}.`
              : `Denied${item.by ? ` by ${item.by.label}` : ''}.`}
        </p>
      )}
    </Card>
  )
}

/** What the user picked for one question: option labels, and their own words when `other`. */
type Choice = { picked: string[]; other: boolean; text: string }

const NO_CHOICE: Choice = { picked: [], other: false, text: '' }

/** The radio value of a question's own-words choice: never an option label, which is text. */
const OTHER = '\u0000other'

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
function attentionTimer(a: Exclude<NonNullable<QuestionItem['attention']>, DoneAttention>): string {
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
      // #1220: the wait is the turn's, and a restart of ScadBuddy ends the turn.
      return `It waits for you until ${when.replace(/^by /, '')}, then stops. A restart of ScadBuddy stops it sooner.`
  }
}

/**
 * #815 §4 — the agent's `done` summary: its message, ScadBuddy's own list of what the
 * turn touched, and one Dismiss button. Nothing waits on it, so there is nothing to
 * reply: dismissing answers it with its first option, which takes it off the badge.
 */
function DoneCard({ item, summary, onAnswer }: { item: QuestionItem; summary: string; onAnswer: (answers: string[]) => void }) {
  const headingId = `question-${item.id}`
  const message = item.questions[0]?.question ?? ''
  const dismiss = item.questions[0]?.options[0]?.label ?? 'Dismiss'
  return (
    <Card size="sm" role="region" aria-labelledby={headingId} className="gap-1.5 px-3 text-[13px]" data-testid="agent-done">
      <h3 id={headingId} className="text-[13px] font-semibold">
        The assistant is done
      </h3>
      <p className="whitespace-pre-wrap">{message}</p>
      <div className="rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px]" data-testid="agent-done-summary">
        <p className="mb-1 text-[11.5px] text-muted-foreground">What ScadBuddy recorded this turn doing</p>
        <Markdown text={summary} />
      </div>
      {item.state === 'pending' ? (
        <div className="mt-0.5" data-agent-user-only="">
          <Button type="button" size="sm" onClick={() => onAnswer([dismiss])}>
            {dismiss}
          </Button>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground" role="status">
          {item.state === 'sent'
            ? 'Dismissing…'
            : item.state === 'answered'
              ? `Dismissed${item.by ? ` by ${item.by.label}` : ''}.`
              : `Closed: ${item.reason ?? 'replaced by a newer summary'}.`}
        </p>
      )}
    </Card>
  )
}

/**
 * #940 — the agent asks the user (AskUserQuestion, or a subagent's `ask_user`): pick an option, or several when
 * the question allows it, or answer in your own words. A question with a draft (an
 * option's `preview`) shows it as Markdown, and its own-words choice is "Edit…",
 * starting from that draft, so editing it returns the edited text.
 */
function QuestionCard({
  item,
  askedBy,
  onAnswer,
}: {
  item: QuestionItem
  askedBy: string | undefined
  onAnswer: (answers: string[]) => void
}) {
  const [choices, setChoices] = useState<Choice[]>(() => item.questions.map(() => NO_CHOICE))
  const headingId = `question-${item.id}`
  const answers = item.questions.map((q, i) => answerOf(q, choices[i] ?? NO_CHOICE))
  const complete = answers.every((a) => a !== undefined)
  const update = (i: number, next: (c: Choice) => Choice) =>
    setChoices((all) => all.map((c, j) => (j === i ? next(c) : c)))
  return (
    <Card
      size="sm"
      role="region"
      aria-labelledby={headingId}
      className="gap-1.5 bg-primary/5 px-3 text-[13px] ring-primary/40"
      data-testid={item.attention ? 'agent-attention' : 'agent-question'}
    >
      <h3 id={headingId} className="text-[13px] font-semibold">
        {item.attention
          ? `The assistant needs you: ${item.questions[0]?.header ?? ''}`
          : item.questions.length === 1
            ? 'A question for you'
            : 'Questions for you'}
      </h3>
      {askedBy !== undefined && (
        // #1109: any subagent may ask, a plugin package's too; its card must not read as the assistant's own.
        <p className="text-xs text-muted-foreground" data-testid="agent-question-asked-by">
          {askedBy ? `Asked by the subagent ${askedBy}, not the assistant itself.` : 'Asked by a subagent, not the assistant itself.'}
        </p>
      )}
      {item.attention && !isDone(item.attention) && item.state === 'pending' && (
        <p className="text-xs text-muted-foreground" data-testid="agent-attention-timer">
          {attentionTimer(item.attention)}
        </p>
      )}
      {item.state === 'pending' ? (
        <form
          className="space-y-3"
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
            const pick = (label: string) =>
              update(i, (c) =>
                q.multiSelect
                  ? { ...c, picked: c.picked.includes(label) ? c.picked.filter((l) => l !== label) : [...c.picked, label] }
                  : { ...c, picked: [label], other: false },
              )
            const writeOwn = () =>
              update(i, (c) => ({
                picked: q.multiSelect ? c.picked : [],
                other: q.multiSelect ? !c.other : true,
                text: c.text || (draft ?? ''),
              }))
            const optionId = (n: number | 'other') => `${name}-${n}`
            const option = (id: string, label: string, description?: string) => (
              <Label htmlFor={id} className="block cursor-pointer text-[13px] leading-snug font-normal">
                {label}
                {description && <span className="block text-[11.5px] text-muted-foreground">{description}</span>}
              </Label>
            )
            return (
              <fieldset key={name} className="space-y-1.5">
                <legend className="text-[13px]">
                  {q.header && (
                    <Badge variant="secondary" className="mr-1.5 align-middle">
                      {q.header}
                    </Badge>
                  )}
                  {q.question}
                </legend>
                {preview !== undefined && (
                  <div
                    className="max-h-72 overflow-y-auto rounded-md border border-border bg-background px-2.5 py-2"
                    data-testid="agent-question-preview"
                  >
                    <Markdown text={preview} />
                  </div>
                )}
                {q.multiSelect ? (
                  <div className="grid gap-2">
                    {q.options.map((o, n) => (
                      <div key={o.label} className="flex items-start gap-2">
                        <Checkbox
                          id={optionId(n)}
                          className="mt-0.5"
                          checked={choice.picked.includes(o.label)}
                          onCheckedChange={() => pick(o.label)}
                        />
                        {option(optionId(n), o.label, o.description)}
                      </div>
                    ))}
                    <div className="flex items-start gap-2">
                      <Checkbox id={optionId('other')} className="mt-0.5" checked={choice.other} onCheckedChange={writeOwn} />
                      {option(optionId('other'), ownWords)}
                    </div>
                  </div>
                ) : (
                  <RadioGroup
                    name={name}
                    value={choice.other ? OTHER : (choice.picked[0] ?? '')}
                    onValueChange={(value) => (value === OTHER ? writeOwn() : pick(value))}
                  >
                    {q.options.map((o, n) => (
                      <div key={o.label} className="flex items-start gap-2">
                        <RadioGroupItem id={optionId(n)} value={o.label} className="mt-0.5" />
                        {option(optionId(n), o.label, o.description)}
                      </div>
                    ))}
                    <div className="flex items-start gap-2">
                      <RadioGroupItem id={optionId('other')} value={OTHER} className="mt-0.5" />
                      {option(optionId('other'), ownWords)}
                    </div>
                  </RadioGroup>
                )}
                {choice.other && (
                  <Textarea
                    aria-label="Your answer"
                    maxLength={ANSWER_MAX}
                    rows={draft === undefined ? 2 : 6}
                    className="field-sizing-fixed text-[13px] md:text-[13px]"
                    value={choice.text}
                    onChange={(e) => update(i, (c) => ({ ...c, text: e.target.value }))}
                  />
                )}
                {(rawAnswerOf(q, choice)?.length ?? 0) > ANSWER_MAX && (
                  <p className="text-xs text-warn" role="alert">
                    This answer is longer than the assistant takes ({ANSWER_MAX.toLocaleString()} characters). Shorten it to send.
                  </p>
                )}
              </fieldset>
            )
          })}
          <Button type="submit" size="sm" disabled={!complete}>
            {item.attention ? 'Send reply' : 'Send answer'}
          </Button>
        </form>
      ) : (
        <div className="space-y-1">
          {item.questions.map((q, i) => (
            <p key={i} className="text-[12.5px]">
              {q.question}
            </p>
          ))}
          <p className="text-xs text-muted-foreground" role="status">
            {item.state === 'sent'
              ? 'Sending your answer…'
              : item.state === 'closed'
                ? `Your answer was not taken: ${item.reason ?? 'the request is no longer waiting'}.`
                : item.state === 'answered'
                ? `Answered${item.by ? ` by ${item.by.label}` : ''}: ${(item.answers ?? []).join(' · ')}`
                : item.reconnected
                  ? 'The tab is back. A change that was waiting was not made; the assistant is told to check the page first.'
                  : item.attention
                  ? `No reply: ${item.reason ?? 'the request was cancelled'}.`
                  : `Not answered: ${item.reason ?? 'the question was cancelled'}.`}
          </p>
        </div>
      )}
    </Card>
  )
}

/**
 * #794 — shown on hover, and always to the keyboard (it is in the tab order) and on
 * touch screens, which have no hover.
 */
function ForkHere({ onClick, title }: { onClick: () => void; title: string }) {
  return (
    <Tip content={title}>
      <Button
        variant="outline"
        size="xs"
        onClick={onClick}
        data-agent-user-only=""
        className="absolute -top-3 right-0 bg-background text-[11px] text-muted-foreground opacity-0 group-hover:opacity-100 focus:opacity-100 [@media(hover:none)]:opacity-100"
      >
        <GitForkIcon data-icon="inline-start" />
        Fork from here
      </Button>
    </Tip>
  )
}

export function FeedItemView({
  item,
  onDecide,
  onAnswer,
  advanced = false,
  askedBy,
  sessionId = '',
  onForkHere,
}: {
  item: FeedItem
  onDecide: (approvalId: string, approve: boolean) => void
  /** #940 — the user's answer to a question: one per question, in order. */
  onAnswer: (questionId: string, answers: string[]) => void
  /** The panel's Advanced switch: every detail, open. Off, only the basics. */
  advanced?: boolean
  /** #1109 — a question's subagent (`askedBy` in chat/state.ts); undefined for the session's own agent. */
  askedBy?: string | undefined
  /** #782 — the session the item is in, where its tool images are served. */
  sessionId?: string
  /**
   * #794 — "Fork from here" on a message: a new chat with the conversation up to it.
   * Absent where there is nothing to fork up to (the first message, a reply still streaming).
   */
  onForkHere?: (() => void) | undefined
}) {
  switch (item.kind) {
    case 'user':
      return (
        <div data-feed-item="user" className="group relative ml-8 rounded-lg bg-ui-muted px-2.5 py-1.5 text-[13px]">
          {item.author.kind !== 'browser' && (
            <p className="mb-0.5 text-[11px] text-muted-foreground">{item.author.label}</p>
          )}
          {item.images && <SentImages images={item.images} sessionId={sessionId} />}
          <p className="whitespace-pre-wrap">{item.text}</p>
          {onForkHere && (
            <ForkHere
              onClick={onForkHere}
              title="Start a new chat with the conversation before this message, and this message to edit and send"
            />
          )}
        </div>
      )
    case 'assistant':
      return (
        <div data-feed-item="assistant" className="group relative text-[13px] leading-relaxed" data-testid="agent-text">
          <Markdown text={item.text} />
          {!item.done && (
            <span
              aria-hidden="true"
              className="ml-0.5 inline-block h-3 w-1.5 bg-primary align-middle motion-safe:animate-pulse"
            />
          )}
          {onForkHere && (
            <ForkHere onClick={onForkHere} title="Start a new chat with the conversation up to this reply" />
          )}
        </div>
      )
    case 'tool':
      // The panel shows calls in groups (ToolGroup); one shown alone has its own status.
      return (
        <ToolCallView
          call={item}
          status={item.result ? (item.result.ok ? 'done' : 'failed') : 'running'}
          sessionId={sessionId}
          advanced={advanced}
        />
      )
    case 'approval':
      return <ApprovalCard item={item} onDecide={(approve) => onDecide(item.id, approve)} />
    case 'question':
      return isDone(item.attention) ? (
        <DoneCard item={item} summary={item.attention.summary} onAnswer={(answers) => onAnswer(item.id, answers)} />
      ) : (
        <QuestionCard item={item} askedBy={askedBy} onAnswer={(answers) => onAnswer(item.id, answers)} />
      )
    case 'memory':
      return <MemoryLine item={item} advanced={advanced} />
    case 'file':
      return (
        <p className="text-[11.5px] text-muted-foreground" data-testid="agent-file">
          {item.by.label} {item.change}{' '}
          <Tip content={item.path}>
            <span className="font-mono break-all">{item.path}</span>
          </Tip>
          {item.size !== undefined && <> ({formatBytes(item.size)})</>}
          {item.change === 'uploaded' && ' to the workspace'}
          {item.change === 'deleted' && ' from the workspace'}
        </p>
      )
    case 'error':
      return (
        <p role="alert" className="text-xs text-warn">
          {item.message}
        </p>
      )
  }
}
