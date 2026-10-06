import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { useLocation } from 'react-router'
import { bridge } from '../../agent/bridge'
import { statusLabel } from '../../agent/chat/labels'
import { pageContext, suggestedPrompts } from '../../agent/chat/pageContext'
import { isDone } from '../../agent/chat/protocol'
import { isBusy, isOwnedByBrowser, type SessionState } from '../../agent/chat/state'
import type { ChatTransportFactory } from '../../agent/chat/transport'
import { useAgentChat } from '../../agent/chat/useAgentChat'
import { useSpeakReplies } from '../../agent/chat/voice'
import { api, ApiError } from '../../api/client'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { OriginBadge, OwnerBadge } from './badges'
import { FeedItemView } from './FeedItemView'
import { BudgetMeter, BudgetSpent, usd } from './SessionBudget'
import { SessionTouched } from './SessionTouched'
import { useDictation, useSpokenReplies } from './useVoice'
import { MicButton, SpeakRepliesToggle, VoiceDisclosure } from './VoiceControls'

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

function formatCost(amount: number): string {
  return amount < 0.01 ? '<$0.01' : usd(amount)
}

/** What a failed agent call says, for the budget card. */
function reason(caught: unknown): Error {
  return new Error(caught instanceof ApiError ? caught.detail : 'The assistant service did not answer; try again.')
}

interface Props {
  factory: ChatTransportFactory
  onClose: () => void
  /** Bumped each time the panel opens, so the composer takes focus. */
  focusKey: number
  /** Inside Bambuddy's iframe, where the microphone may be blocked (#257). */
  embedded?: boolean
  /** #931 — a session a page asked to open; selected once, then `onOpenHandled` clears it. */
  openRequest?: OpenRequest | null
  onOpenHandled?: () => void
}

export interface OpenRequest {
  sessionId: string
}

/** The assistant panel's body: sessions, the stream and action feed, and the composer. */

/** How many sessions the picker's model filter asks the agent for: its list route's maximum (agent routes/sessions.ts LIST_LIMIT_MAX). */
const PICKER_FILTER_LIMIT = 500

/** The panel's Advanced switch, remembered per browser as the Library page's is. */
const ADVANCED_KEY = 'scadbuddy.assistant.advanced'

function readAdvanced(): boolean {
  try {
    return window.localStorage.getItem(ADVANCED_KEY) === '1'
  } catch {
    return false
  }
}

export function AssistantChat({ factory, onClose, focusKey, embedded = false, openRequest, onOpenHandled }: Props) {
  const chat = useAgentChat(factory)
  const { state } = chat
  const { pathname } = useLocation()
  const [draft, setDraft] = useState('')
  const [pickerOpen, setPickerOpen] = useState(false)
  const pageModel = pageContext(pathname).modelSlug ?? null
  // The model the filter was turned on for: on another model's page it is off.
  const [filteredModel, setFilteredModel] = useState<string | null>(null)
  const filterBy = pageModel !== null && filteredModel === pageModel ? pageModel : null
  // Read again each time the picker opens or the filter is turned on, so it is current.
  const touching = useAsync(
    async () =>
      filterBy && pickerOpen
        ? new Set(
            (await api.listAiResourceSessions({ type: 'model', id: filterBy }, PICKER_FILTER_LIMIT)).sessions.map(
              (s) => s.id,
            ),
          )
        : null,
    [filterBy, pickerOpen],
  )
  // #931 — the active session's "Touched" panel.
  const [touchedOpen, setTouchedOpen] = useState(false)
  const [advanced, setAdvanced] = useState(readAdvanced)
  function toggleAdvanced() {
    setAdvanced((was) => {
      const next = !was
      try {
        window.localStorage.setItem(ADVANCED_KEY, next ? '1' : '0')
      } catch {
        // Private mode or blocked storage: the switch still works for this page.
      }
      return next
    })
  }
  const composer = useRef<HTMLTextAreaElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const takeOverButton = useRef<HTMLButtonElement>(null)
  const feedEnd = useRef<HTMLDivElement>(null)
  const pickerId = useId()
  const touchedId = useId()
  const voiceNoteId = useId()

  const active: SessionState | undefined = state.activeId ? state.sessions[state.activeId] : undefined
  const busy = isBusy(active) || state.awaitingStart
  const owned = !active || isOwnedByBrowser(active)
  const streaming = active?.items.some((i) => i.kind === 'assistant' && !i.done) ?? false
  const pendingApproval = active?.items.some((i) => i.kind === 'approval' && i.state === 'pending') ?? false
  const pendingQuestion = active?.items.some((i) => i.kind === 'question' && i.state === 'pending' && !i.attention) ?? false
  // A `done` summary (#815 §4) asks nothing of the user: it is not announced as needing them.
  const pendingAttention =
    active?.items.some((i) => i.kind === 'question' && i.state === 'pending' && i.attention && !isDone(i.attention)) ?? false
  const itemCount = active?.items.length ?? 0
  const finishedTools = active?.items.filter((i) => i.kind === 'tool' && i.result).length ?? 0

  // Voice (#257): dictation fills the draft for the user to review; replies can be read aloud.
  // Kept in step with every write, so dictation reconciles against typing that hasn't
  // rendered yet rather than a stale value.
  const draftNow = useRef('')
  const writeDraft = useCallback((text: string) => {
    draftNow.current = text
    setDraft(text)
  }, [])
  const getDraft = useCallback(() => draftNow.current, [])
  const focusComposer = useCallback(() => composer.current?.focus(), [])
  const speakReplies = useSpeakReplies()
  const speech = useSpokenReplies(state.activeId, active?.items, speakReplies)
  const dictation = useDictation({
    getDraft,
    setDraft: writeDraft,
    onDone: focusComposer,
    onStart: speech.stop,
    embedded,
  })

  useEffect(() => {
    composer.current?.focus()
  }, [focusKey])

  // #992 — what was focused (Stop, a session's row) goes away, and focus with it to
  // <body>. Asked for here, it lands after the render that shows the session: in the
  // composer, or on Take over when another agent holds it (the composer is locked).
  const [refocus, setRefocus] = useState(0)
  const focusSession = () => setRefocus((n) => n + 1)
  useEffect(() => {
    if (refocus === 0) return
    const box = composer.current
    const target = box && !box.disabled ? box : (takeOverButton.current ?? heading.current)
    target?.focus()
  }, [refocus])
  // Take over goes, and the composer unlocks, only once the handoff lands.
  const handingOver = useRef(false)
  useEffect(() => {
    if (owned && handingOver.current) {
      handingOver.current = false
      composer.current?.focus()
    }
  }, [owned])

  // Before the socket is open this only sets what is on screen; the connection attaches it.
  const { select } = chat
  useEffect(() => {
    if (!openRequest) return
    setPickerOpen(false)
    select(openRequest.sessionId)
    onOpenHandled?.()
    focusSession()
  }, [openRequest, select, onOpenHandled])

  // Follow the stream. Instant when the user asked for reduced motion.
  const lastText = active?.items.at(-1)
  useEffect(() => {
    feedEnd.current?.scrollIntoView?.({
      block: 'end',
      behavior: prefersReducedMotion() ? 'auto' : 'smooth',
    })
  }, [itemCount, lastText])

  const submit = (text: string) => {
    if (!text.trim() || busy || !owned) return
    dictation.cancel()
    speech.arm()
    const { tools, dialogs, page } = bridge.snapshot()
    chat.send(text, pageContext(pathname, { tools, dialogs, page }))
    writeDraft('')
  }

  const onSubmit = (event: FormEvent) => {
    event.preventDefault()
    submit(draft)
  }

  const onComposerKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit(draft)
    }
  }

  const startNewChat = () => {
    chat.select(null)
    setPickerOpen(false)
    composer.current?.focus()
  }

  // #790 — a chat that used its budget. Forking copies the transcript into a new
  // session with a fresh budget; the panel then shows that one (attach replays it).
  const continueInNewChat = async (id: string) => {
    try {
      const { session } = await api.forkAiSession(id)
      chat.select(session.id)
      composer.current?.focus()
    } catch (caught) {
      throw reason(caught)
    }
  }
  // The raised budget arrives over the socket (`session.budget`), which clears the card.
  const raiseBudget = async (id: string, addUsd: number) => {
    try {
      await api.raiseAiSessionBudget(id, addUsd)
    } catch (caught) {
      throw reason(caught)
    }
  }

  const prompts = suggestedPrompts(pathname)
  const sessions = state.order.map((id) => state.sessions[id]).filter((s): s is SessionState => !!s)
  // #931 — on a model's page, the picker can show only the sessions that changed that model.
  const touchingIds = filterBy ? touching.data : null
  const listed = touchingIds ? sessions.filter((s) => touchingIds.has(s.id)) : sessions
  const filterLoading = filterBy !== null && touching.loading
  const filterError = filterBy !== null ? touching.error : undefined

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center gap-1.5 border-b border-line px-3 py-2">
        <h2 ref={heading} tabIndex={-1} className="text-[13px] font-semibold outline-none">
          Assistant
        </h2>
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            aria-expanded={pickerOpen}
            aria-controls={pickerId}
            onClick={() => setPickerOpen((o) => !o)}
          >
            Sessions ({sessions.length})
          </Button>
          <Button variant="ghost" size="sm" onClick={startNewChat}>
            New chat
          </Button>
          <Button
            variant="ghost"
            size="sm"
            aria-pressed={advanced}
            title="Show every tool call's arguments and sources, and what memory recalled and saved"
            onClick={toggleAdvanced}
          >
            Advanced
          </Button>
          <Button variant="ghost" size="sm" aria-label="Close assistant" onClick={onClose}>
            ✕
          </Button>
        </div>
      </header>

      {pickerOpen && (
        <nav id={pickerId} aria-label="Sessions" className="shrink-0 border-b border-line bg-surface-2">
          {pageModel && (
            <label className="flex items-center gap-1.5 px-3 pt-2 text-[12px] text-muted">
              <input
                type="checkbox"
                checked={filterBy !== null}
                onChange={(event) => setFilteredModel(event.target.checked ? pageModel : null)}
              />
              Only sessions that changed {pageModel}
            </label>
          )}
          {filterError ? (
            <p role="alert" className="px-3 pt-1 text-[12px] text-warn">
              {filterError instanceof ApiError ? filterError.detail : 'The assistant service did not answer.'} Showing
              every session, unfiltered.
            </p>
          ) : null}
          {touchingIds && touchingIds.size >= PICKER_FILTER_LIMIT ? (
            <p className="px-3 pt-1 text-[11px] text-faint">
              Checked against the {PICKER_FILTER_LIMIT} most recently updated sessions that changed {filterBy}.
            </p>
          ) : null}
          {sessions.length === 0 ? (
            <p className="px-3 py-2 text-[12.5px] text-muted">No sessions yet.</p>
          ) : filterLoading ? (
            <p role="status" className="px-3 py-2 text-[12.5px] text-muted">
              Finding the sessions that changed {filterBy}…
            </p>
          ) : touchingIds && listed.length === 0 ? (
            <p className="px-3 py-2 text-[12.5px] text-muted">
              {touchingIds.size === 0
                ? `No session changed ${filterBy}.`
                : // The ones that did are older than the sessions this panel has loaded.
                  `None of the loaded sessions changed ${filterBy}.`}
            </p>
          ) : (
            <ul className="max-h-56 overflow-y-auto py-1">
              {listed.map((s) => (
                <li key={s.id}>
                  <button
                    type="button"
                    aria-current={s.id === state.activeId ? 'true' : undefined}
                    onClick={() => {
                      chat.select(s.id)
                      setPickerOpen(false)
                      focusSession()
                    }}
                    className={`flex w-full flex-col gap-1 border-l-2 px-3 py-1.5 text-left hover:bg-surface-3 ${
                      s.id === state.activeId ? 'border-accent bg-surface-3' : 'border-transparent'
                    }`}
                  >
                    <span className="truncate text-[12.5px]">{s.title}</span>
                    <span className="flex items-center gap-1.5">
                      {s.id === state.activeId && (
                        <span className="rounded-[4px] bg-accent/15 px-1 text-[10.5px] font-medium text-accent">Open</span>
                      )}
                      <OriginBadge origin={s.origin} />
                      <OwnerBadge owner={s.owner} />
                      <span className="text-[11px] text-faint">{statusLabel(s.status)}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </nav>
      )}

      {active && (
        <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-line px-3 py-1.5 text-[12px]">
          <span className="min-w-0 flex-1 truncate font-medium" title={active.title}>
            {active.title}
          </span>
          <OriginBadge origin={active.origin} />
          <span className="text-faint" data-testid="agent-status">
            {statusLabel(active.status)}
          </span>
          {busy && (
            <Button
              variant="danger"
              size="sm"
              onClick={() => {
                chat.interrupt(active.id)
                // The button goes once the turn has stopped; the next thing is a message.
                focusSession()
              }}
            >
              Stop
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            aria-expanded={touchedOpen}
            aria-controls={touchedOpen ? touchedId : undefined}
            title="What this session's tool calls created, changed or deleted"
            onClick={() => setTouchedOpen((o) => !o)}
          >
            Touched
          </Button>
          <BudgetMeter session={active} />
          {!owned && (
            <div className="flex w-full items-center gap-2">
              <span className="text-muted">Controlled by {active.owner.label}</span>
              <Button
                ref={takeOverButton}
                size="sm"
                data-agent-user-only=""
                onClick={() => {
                  handingOver.current = true
                  chat.takeOver(active.id)
                }}
              >
                Take over
              </Button>
            </div>
          )}
        </div>
      )}

      {active && touchedOpen && (
        <section
          id={touchedId}
          aria-label="What this session touched"
          className="shrink-0 border-b border-line bg-surface-2"
        >
          {/* Read again when the status moves or a tool call finishes, so a running turn's
              changes show as they land. Keyed per session, so a switch reads once. */}
          <SessionTouched
            key={active.id}
            sessionId={active.id}
            refreshKey={`${active.status}:${finishedTools}`}
          />
        </section>
      )}

      <div
        role="log"
        aria-label="Conversation"
        aria-live="polite"
        aria-busy={streaming}
        className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3"
      >
        {state.notice && (
          <p role="alert" className="text-[12.5px] text-warn">
            {state.notice}
          </p>
        )}
        {active?.items.map((item) => (
          <FeedItemView
            key={`${item.kind}-${item.id}`}
            item={item}
            advanced={advanced}
            onDecide={(approvalId, approve) => chat.decide(active.id, approvalId, approve)}
            onAnswer={(questionId, answers) => chat.answer(active.id, questionId, answers)}
          />
        ))}
        {itemCount === 0 && !busy && advanced && (
          // #1488 — an empty chat has no feed for Advanced to change, so say what it will do.
          <p className="text-[12px] text-muted">
            Advanced: tool arguments, sources and memory details will be shown.
          </p>
        )}
        {itemCount === 0 && !busy && prompts.length > 0 && owned && (
          <div>
            <p className="mb-2 text-[12px] text-muted">Try asking</p>
            <ul className="flex flex-col items-start gap-1.5">
              {prompts.map((prompt) => (
                <li key={prompt}>
                  <Button size="sm" data-agent-user-only="" onClick={() => submit(prompt)}>
                    {prompt}
                  </Button>
                </li>
              ))}
            </ul>
          </div>
        )}
        {active?.budgetSpent && !busy && owned && (
          <BudgetSpent
            // A new card, with its own amount and errors, for each chat.
            key={active.id}
            session={active}
            onContinue={() => continueInNewChat(active.id)}
            onRaise={(addUsd) => raiseBudget(active.id, addUsd)}
            onStartNew={startNewChat}
          />
        )}
        {active?.result && !busy && (
          <p className="text-[11px] text-faint">
            {active.result.turns} turn{active.result.turns === 1 ? '' : 's'}
            {active.result.costUsd !== undefined && ` · ${formatCost(active.result.costUsd)}`}
          </p>
        )}
        <div ref={feedEnd} />
      </div>

      {/* Announced once per change, unlike the stream itself. */}
      <p className="sr-only" role="status">
        {pendingApproval
          ? 'The assistant needs your approval.'
          : pendingQuestion
            ? 'The assistant has a question for you.'
            : pendingAttention
              ? 'The assistant needs your attention.'
              : ''}
      </p>

      {/* The user's own voice: the bridge's fill/click never type or send here (#254). */}
      <form onSubmit={onSubmit} data-agent-user-only="" className="shrink-0 border-t border-line p-2.5">
        <label htmlFor="assistant-composer" className="sr-only">
          Message the assistant
        </label>
        <textarea
          id="assistant-composer"
          ref={composer}
          rows={2}
          value={draft}
          onChange={(event) => writeDraft(event.target.value)}
          onKeyDown={onComposerKey}
          disabled={!owned}
          placeholder={
            owned ? 'Ask about this page… (Enter to send, Shift+Enter for a new line)' : 'Take over to send messages'
          }
          className="w-full resize-none rounded-[6px] border border-line bg-bg px-2.5 py-1.5 text-[13px] outline-none focus:border-line-strong disabled:opacity-50"
        />
        {dictation.error && (
          <p role="alert" className="mt-1 text-[12px] text-warn">
            {dictation.error}
          </p>
        )}
        <p className="sr-only" role="status">
          {dictation.announcement}
        </p>
        <div className="mt-1.5 flex items-center justify-end gap-2">
          <div className="mr-auto flex items-center gap-2">
            <SpeakRepliesToggle />
            {speech.speaking && (
              <Button size="sm" variant="ghost" onClick={speech.stop}>
                Stop speaking
              </Button>
            )}
          </div>
          {busy && <span className="text-[11.5px] text-faint">Wait for this turn to finish, or stop it.</span>}
          <MicButton dictation={dictation} disabled={!owned} embedded={embedded} describedBy={voiceNoteId} />
          <Button type="submit" variant="primary" size="sm" disabled={!draft.trim() || busy || !owned}>
            Send
          </Button>
        </div>
        <VoiceDisclosure id={voiceNoteId} />
      </form>
    </div>
  )
}
