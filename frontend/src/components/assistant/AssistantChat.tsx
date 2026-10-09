import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
} from 'react'
import { useLocation } from 'react-router'
import { bridge } from '../../agent/bridge'
import {
  IMAGE_ACCEPT,
  IMAGES_DATA_TOTAL_MAX,
  IMAGES_MAX,
  composerImages,
  dataUrl,
  prepareImage,
} from '../../agent/chat/images'
import { statusLabel } from '../../agent/chat/labels'
import { pageContext, suggestedPrompts } from '../../agent/chat/pageContext'
import { isDone, type UserImage } from '../../agent/chat/protocol'
import { askedBy, isBusy, isOwnedByBrowser, type SessionPatch, type SessionState } from '../../agent/chat/state'
import { feedBlocks, toolStatus } from '../../agent/chat/toolGroups'
import type { ChatTransportFactory } from '../../agent/chat/transport'
import { useAgentChat } from '../../agent/chat/useAgentChat'
import { useSpeakReplies } from '../../agent/chat/voice'
import { api, ApiError } from '../../api/client'
import type { AiSessionView } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { OriginBadge } from './badges'
import { FeedItemView } from './FeedItemView'
import { ToolGroup } from './ToolGroup'
import { BudgetMeter, BudgetSpent, usd } from './SessionBudget'
import { SessionSwitcher } from './SessionSwitcher'
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

/** #792 — a session route's answer, as the panel's state keeps it. */
function patchOf(view: AiSessionView): SessionPatch {
  return {
    id: view.id,
    title: view.title,
    status: view.status,
    parentId: view.parent_id,
    updatedAt: view.updated_at,
    costUsd: view.cost_usd,
    budgetUsd: view.budget_usd,
  }
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

/**
 * #1866 — an image waiting in the composer to go with the next message: uploaded when it
 * was attached (#1941), so the message sends `id`; `image` is kept for the caps and the
 * thumbnail.
 */
interface Attached {
  key: number
  name: string
  image: UserImage
  id: string
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
  // The model the filter was turned on for. Every page starts unfiltered, a page left and
  // come back to included (#1340).
  const [filteredModel, setFilteredModel] = useState<string | null>(null)
  const [filterPage, setFilterPage] = useState(pageModel)
  if (filterPage !== pageModel) {
    setFilterPage(pageModel)
    setFilteredModel(null)
  }
  const filterBy = pageModel !== null && filteredModel === pageModel ? pageModel : null
  const filtering = filterBy !== null && pickerOpen
  // Read again each time the picker opens or the filter is turned on, and while it is open,
  // on the model's changes and on every tool result the panel sees, so a session that
  // changes the model meanwhile is listed (#1340).
  const touching = useAsync(
    async () =>
      filtering
        ? new Set(
            (await api.listAiResourceSessions({ type: 'model', id: filterBy }, PICKER_FILTER_LIMIT)).sessions.map(
              (s) => s.id,
            ),
          )
        : null,
    [filterBy, pickerOpen],
    filtering ? [`model:${filterBy}`] : [],
  )
  const toolResults = useMemo(
    () =>
      Object.values(state.sessions).reduce(
        (count, s) => count + (s?.items.filter((item) => item.kind === 'tool' && item.result).length ?? 0),
        0,
      ),
    [state.sessions],
  )
  const refreshTouching = touching.refresh
  useEffect(() => {
    if (filtering) refreshTouching()
    // Only a new tool result re-reads; opening the picker or the filter reads through `touching`'s deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toolResults])
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

  // #1866 — images pasted, dropped or attached, sent with the next message. Kept in a
  // ref too, as the draft is, so images prepared at once all count against the caps.
  const [attached, setAttached] = useState<Attached[]>([])
  const attachedNow = useRef<Attached[]>([])
  const writeAttached = useCallback((next: Attached[]) => {
    attachedNow.current = next
    setAttached(next)
  }, [])
  const [preparing, setPreparing] = useState(0)
  const preparingNow = useRef(0)
  const nextKey = useRef(0)
  const [imageErrors, setImageErrors] = useState<string[]>([])
  const [dropping, setDropping] = useState(false)
  const filePicker = useRef<HTMLInputElement>(null)
  // The long edge images are scaled to, from Settings; the default until it answers, or if it cannot.
  const imageEdge = useAsync(() => api.getImageSettings(), []).data?.long_edge
  const addImages = async (files: File[]) => {
    if (files.length === 0) return
    const room = Math.max(0, IMAGES_MAX - attachedNow.current.length - preparingNow.current)
    const taken = files.slice(0, room)
    const errors = files
      .slice(room)
      .map((file) => `At most ${IMAGES_MAX} images per message: ${file.name} was not added.`)
    preparingNow.current += taken.length
    setPreparing(preparingNow.current)
    const results = await Promise.allSettled(taken.map((file) => prepareImage(file, undefined, imageEdge)))
    const ready: { name: string; image: UserImage }[] = []
    let total = attachedNow.current.reduce((sum, a) => sum + a.image.data.length, 0)
    results.forEach((result, index) => {
      const name = taken[index]?.name ?? 'image'
      if (result.status === 'rejected') {
        errors.push(result.reason instanceof Error ? result.reason.message : `${name} could not be added.`)
      } else if (total + result.value.data.length > IMAGES_DATA_TOTAL_MAX) {
        errors.push(`${name} was not added: this message's images would be too large together.`)
      } else {
        total += result.value.data.length
        ready.push({ name, image: result.value })
      }
    })
    // #1941 — each image goes to the agent now, and the message sends its id.
    const uploads = await Promise.allSettled(ready.map(({ image }) => api.uploadAttachment(image)))
    preparingNow.current -= taken.length
    setPreparing(preparingNow.current)
    const added: Attached[] = []
    uploads.forEach((upload, index) => {
      const { name, image } = ready[index]!
      if (upload.status === 'rejected') {
        const why = upload.reason instanceof ApiError ? upload.reason.detail : 'the assistant service did not answer'
        errors.push(`${name} could not be uploaded: ${why}`)
      } else {
        added.push({ key: nextKey.current++, name, image, id: upload.value.id })
      }
    })
    writeAttached([...attachedNow.current, ...added])
    setImageErrors(errors)
  }
  const removeImage = (key: number) => {
    const removed = attachedNow.current.find((a) => a.key === key)
    // Best effort: an upload left behind expires on its own.
    if (removed) api.deleteAttachment(removed.id).catch(() => {})
    writeAttached(attachedNow.current.filter((a) => a.key !== key))
    setImageErrors([])
    composer.current?.focus()
  }
  // Only while the composer has focus: a paste elsewhere is the page's (a model's media, #722).
  const onComposerPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    // Copied cells or a web page carry text too: that paste is the text box's.
    if (event.clipboardData.getData('text/plain')) return
    const files = composerImages(event.clipboardData)
    if (files.length === 0) return
    event.preventDefault()
    void addImages(files)
  }
  const carriesFiles = (event: DragEvent) => Array.from(event.dataTransfer.types ?? []).includes('Files')
  const onComposerDragOver = (event: DragEvent<HTMLFormElement>) => {
    if (!owned || !carriesFiles(event)) return
    event.preventDefault()
    setDropping(true)
  }
  const onComposerDrop = (event: DragEvent<HTMLFormElement>) => {
    setDropping(false)
    if (!owned) return
    const files = composerImages(event.dataTransfer)
    if (files.length === 0) return
    event.preventDefault()
    void addImages(files)
  }
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
    if (!text.trim() || busy || !owned || preparingNow.current > 0) return
    dictation.cancel()
    speech.arm()
    const { tools, dialogs, page } = bridge.snapshot()
    const images = attachedNow.current.map((a) => ({ kind: 'attachment' as const, id: a.id }))
    chat.send(text, pageContext(pathname, { tools, dialogs, page }), images.length ? images : undefined)
    writeDraft('')
    writeAttached([])
    setImageErrors([])
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

  // #790, #794 — a fork copies the transcript (with `upTo`, through that reply) into a new
  // session with a fresh budget; the panel then shows that one (attach replays it).
  // `draft` is the message a user-message fork puts back in the composer, to edit and send.
  const openFork = async (id: string, options: { upTo?: string; draft?: string } = {}) => {
    const { session } = await api.forkAiSession(id, options.upTo === undefined ? {} : { upTo: options.upTo })
    chat.patch(patchOf(session))
    chat.select(session.id)
    if (options.draft !== undefined) writeDraft(options.draft)
    setPickerOpen(false)
    focusSession()
  }
  const continueInNewChat = async (id: string) => {
    try {
      await openFork(id)
    } catch (caught) {
      throw reason(caught)
    }
  }
  const [forkError, setForkError] = useState<string | null>(null)
  const fork = (id: string, options: { upTo?: string; draft?: string } = {}) => {
    setForkError(null)
    openFork(id, options).catch((caught: unknown) => setForkError(reason(caught).message))
  }
  // #795 — rename and done answer with the session, shown at once; a refusal is the row's to show.
  const updateSession = async (id: string, edit: { title: string } | { done: true }) => {
    try {
      chat.patch(patchOf((await api.updateAiSession(id, edit)).session))
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

  // #794 — what "Fork from here" forks up to: a finished reply itself; a user message,
  // the last finished reply before it (none before the first message).
  const forkPoints = new Map<string, { upTo: string; draft?: string }>()
  let lastReply: string | undefined
  for (const item of active?.items ?? []) {
    if (item.kind === 'assistant' && item.done) {
      lastReply = item.id
      forkPoints.set(`assistant:${item.id}`, { upTo: item.id })
    } else if (item.kind === 'user' && lastReply) {
      forkPoints.set(`user:${item.id}`, { upTo: lastReply, draft: item.text })
    }
  }
  const hasReply = lastReply !== undefined
  const parent = active?.parentId ? state.sessions[active.parentId] : undefined

  const prompts = suggestedPrompts(pathname)
  const sessions = state.order.map((id) => state.sessions[id]).filter((s): s is SessionState => !!s)
  // #931 — on a model's page, the picker can show only the sessions that changed that model.
  const touchingIds = filterBy ? touching.data : null
  const listed = touchingIds ? sessions.filter((s) => touchingIds.has(s.id)) : sessions
  const filterLoading = filterBy !== null && touching.loading
  const filterError = filterBy !== null ? touching.error : undefined

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* #1038 — wraps: the buttons outgrow the 380 px panel once "Sessions (N)" reaches two
          digits, and an unwrapped row pushed Close past the viewport's edge. */}
      <header className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-line px-3 py-2">
        <h2 ref={heading} tabIndex={-1} className="text-[13px] font-semibold outline-none">
          Assistant
        </h2>
        <div className="ml-auto flex flex-wrap items-center justify-end gap-1">
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
            <SessionSwitcher
              sessions={listed}
              activeId={state.activeId}
              onOpen={(id) => {
                chat.select(id)
                setPickerOpen(false)
                focusSession()
              }}
              onRename={(id, title) => updateSession(id, { title })}
              onDone={(id) => updateSession(id, { done: true })}
            />
          )}
        </nav>
      )}

      {active && (
        <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-line px-3 py-1.5 text-[12px]">
          <span className="min-w-0 flex-1 truncate font-medium" title={active.title} data-testid="active-session-title">
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
            disabled={!hasReply}
            title={
              hasReply
                ? 'Copy this chat into a new one and continue there; this one stays as it is'
                : 'No reply to fork yet: wait for the assistant to answer'
            }
            data-agent-user-only=""
            onClick={() => fork(active.id)}
          >
            Fork
          </Button>
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
          {active.parentId && (
            <div className="flex w-full items-center gap-1 text-[11.5px] text-muted">
              <button
                type="button"
                className="truncate underline decoration-dotted underline-offset-2 hover:text-ink"
                onClick={() => {
                  chat.select(active.parentId!)
                  focusSession()
                }}
              >
                Forked from {parent?.title ?? 'an earlier chat'}
              </button>
            </div>
          )}
          {forkError && (
            <p role="alert" className="w-full text-warn">
              The chat was not forked: {forkError}
            </p>
          )}
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
        {active &&
          feedBlocks(active.items).map((block) =>
            block.kind === 'tools' ? (
              // #782 — consecutive calls as one group; every other item keeps its own card.
              <ToolGroup
                key={`tools-${block.id}`}
                calls={block.calls}
                statuses={block.calls.map((call) => toolStatus(call, active.items, { settled: !busy }))}
                sessionId={active.id}
                advanced={advanced}
              />
            ) : (
              <FeedItemView
                key={`${block.item.kind}-${block.item.id}`}
                item={block.item}
                advanced={advanced}
                sessionId={active.id}
                askedBy={block.item.kind === 'question' ? askedBy(active.items, block.item) : undefined}
                onForkHere={
                  forkPoints.has(`${block.item.kind}:${block.item.id}`)
                    ? () => fork(active.id, forkPoints.get(`${block.item.kind}:${block.item.id}`) ?? {})
                    : undefined
                }
                onDecide={(approvalId, approve) => chat.decide(active.id, approvalId, approve)}
                onAnswer={(questionId, answers) => chat.answer(active.id, questionId, answers)}
              />
            ),
          )}
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
      <form
        onSubmit={onSubmit}
        onDragOver={onComposerDragOver}
        onDragLeave={() => setDropping(false)}
        onDrop={onComposerDrop}
        data-agent-user-only=""
        className={`shrink-0 border-t p-2.5 ${dropping ? 'border-accent bg-accent/5' : 'border-line'}`}
      >
        <label htmlFor="assistant-composer" className="sr-only">
          Message the assistant
        </label>
        {attached.length > 0 && (
          <ul aria-label="Images to send" className="mb-1.5 flex flex-wrap gap-1.5">
            {attached.map((a) => (
              <li key={a.key} className="relative">
                <img
                  src={dataUrl(a.image.preview)}
                  alt={a.name}
                  className="h-14 w-14 rounded-[4px] border border-line object-cover"
                />
                <button
                  type="button"
                  aria-label={`Remove ${a.name}`}
                  title={`Remove ${a.name}`}
                  onClick={() => removeImage(a.key)}
                  className="absolute -right-1.5 -top-1.5 flex h-6 w-6 items-center justify-center rounded-full border border-line bg-surface-2 text-[11px] leading-none hover:bg-surface-3"
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        )}
        <textarea
          id="assistant-composer"
          ref={composer}
          rows={2}
          value={draft}
          onChange={(event) => writeDraft(event.target.value)}
          onKeyDown={onComposerKey}
          onPaste={onComposerPaste}
          disabled={!owned}
          placeholder={
            owned ? 'Ask about this page… (Enter to send, Shift+Enter for a new line)' : 'Take over to send messages'
          }
          className="w-full resize-none rounded-[6px] border border-line bg-bg px-2.5 py-1.5 text-[13px] outline-none focus:border-line-strong disabled:opacity-50"
        />
        {imageErrors.map((error) => (
          <p key={error} role="alert" className="mt-1 text-[12px] text-warn">
            {error}
          </p>
        ))}
        {preparing > 0 && (
          <p role="status" className="mt-1 text-[11.5px] text-faint">
            Preparing {preparing === 1 ? 'the image' : `${preparing} images`}…
          </p>
        )}
        {attached.length > 0 && !draft.trim() && (
          <p className="mt-1 text-[11.5px] text-faint">Add a message to send with the images.</p>
        )}
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
          <input
            ref={filePicker}
            type="file"
            accept={IMAGE_ACCEPT}
            multiple
            hidden
            aria-label="Attach images"
            onChange={(event) => {
              const files = Array.from(event.target.files ?? [])
              // Cleared, so picking the same file again is a change.
              event.target.value = ''
              void addImages(files)
            }}
          />
          <Button
            size="sm"
            variant="ghost"
            disabled={!owned || attached.length >= IMAGES_MAX}
            title={`Attach images (PNG, JPEG, GIF or WebP; up to ${IMAGES_MAX}). You can also paste or drop them here.`}
            onClick={() => filePicker.current?.click()}
          >
            Image
          </Button>
          <MicButton dictation={dictation} disabled={!owned} embedded={embedded} describedBy={voiceNoteId} />
          <Button type="submit" variant="primary" size="sm" disabled={!draft.trim() || busy || !owned || preparing > 0}>
            Send
          </Button>
        </div>
        <VoiceDisclosure id={voiceNoteId} />
      </form>
    </div>
  )
}
