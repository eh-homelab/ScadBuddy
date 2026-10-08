import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { NavLink, Outlet, useLocation } from 'react-router'
import { attentionCount, attentionDetail, attentionLabel, summaryLabel, useAttention, useAttentionTitle } from '../agent/attention'
import { useAiAvailability } from '../agent/chat/availability'
import {
  ASSISTANT_SHORTCUT_ARIA,
  ASSISTANT_SHORTCUT_LABEL,
  isAssistantShortcut,
} from '../agent/chat/shortcut'
import { AssistantOpenerContext, type AssistantOpener } from '../agent/chat/opener'
import type { ChatTransportFactory } from '../agent/chat/transport'
import type { TabLinkFactory } from './AgentLink'
import { useGlobalAgentTools } from '../agent/global'
import { isEmbedded } from '../lib/embed'
import { ErrorBoundary } from './ErrorBoundary'
import { LiveUpdatesIndicator } from './LiveUpdatesIndicator'
import { Button } from './ui/Button'
import { useLoadBambuddyLinks } from '../lib/bambuddyLinks'
import { useLoadDisplayUnit } from '../lib/units'
import { leaveFullscreen } from '../lib/useFullscreen'
import { ModalCompanionContext, focusInto, topmostDialog } from '../lib/modal'

// Split out: the panel, its protocol schemas (zod) and its renderer download only
// when someone opens it, and never when AI is off.
const AssistantPanel = lazy(async () => ({
  default: (await import('./assistant/AssistantPanel')).AssistantPanel,
}))

const NAV = [
  { to: '/', label: 'Models', end: true },
  { to: '/prints', label: 'Prints', end: false },
  { to: '/library', label: 'Library', end: false },
  { to: '/settings', label: 'Settings', end: false },
]

// #254 — this tab's socket to the agent, and the pairing prompt, while the assistant is
// available; like the panel, loaded only then.
const AgentLink = lazy(async () => ({ default: (await import('./AgentLink')).AgentLink }))

const PANEL_ID = 'assistant-panel'

interface Props {
  embedded?: boolean
  /** Tests inject the mock agent; the app loads whichever this build has. */
  assistantTransport?: ChatTransportFactory
  /** Tests inject the browser bridge's link (null for none); the app makes its own. */
  tabLink?: TabLinkFactory | null
}

export function AppShell({ embedded = isEmbedded(), assistantTransport, tabLink }: Props) {
  const location = useLocation()
  useLoadDisplayUnit()
  useLoadBambuddyLinks()
  // #254 — navigate, snapshot and the click/fill fallbacks, on every route.
  useGlobalAgentTools()
  const ai = useAiAvailability()
  const [open, setOpen] = useState(false)
  // Mounted from the first open on, and hidden rather than unmounted when closed, so
  // closing the panel doesn't drop the connection or the transcript.
  const [mounted, setMounted] = useState(false)
  // #798 — the panel's element, which a dialog leaves usable beside it while it is open.
  const [panelElement, setPanelElement] = useState<HTMLElement | null>(null)
  // #931 — a page asked for one session ("Changed by assistant"): the panel opens on it.
  // Cleared once the panel has selected it, or when the panel goes, so a later mount
  // does not select it.
  const [openRequest, setOpenRequest] = useState<{ sessionId: string } | null>(null)
  // A mounted panel rides out an outage: while the agent restarts, or its status
  // read fails for a moment, the panel's transport keeps reconnecting with its
  // transcript and anything queued for the reconnect (a decision, say) intact,
  // where unmounting would drop them. The panel goes only when the agent says the
  // assistant is off for this page: not set up, or its chat gate refuses this
  // address. Until it is first opened, the agent's answer alone decides.
  const off = ai.state === 'not_configured' || ai.chat === 'refused'
  const shown = ai.available || (mounted && !off)
  useEffect(() => {
    // Gone for good: the next time the agent is available it starts afresh.
    if (!shown) {
      setOpen(false)
      setMounted(false)
      // #931 — a session asked for but not yet selected goes too.
      setOpenRequest(null)
    }
  }, [shown])
  // #815 — what waits on the user (approvals, questions, attention requests), shown on the
  // toggle so a closed panel (or a background session's, which never reaches this tab's
  // socket) still says so.
  const attention = useAttention(shown)
  const refreshAttention = attention.refresh
  const waitingLabel = attentionLabel(attention.waiting)
  const waitingDetail = attentionDetail(attention.counts)
  // A done summary waits for nothing, so it is shown beside the count, not in it.
  const summaries = summaryLabel(attention.counts)
  const toggleLabel = [waitingLabel, summaries].filter(Boolean).join(', ')
  useAttentionTitle(attention.waiting, !embedded)
  const [focusKey, setFocusKey] = useState(0)
  const toggleButton = useRef<HTMLButtonElement>(null)

  const openPanel = useCallback(() => {
    setMounted(true)
    setOpen(true)
    setFocusKey((k) => k + 1)
  }, [])
  const openHandled = useCallback(() => setOpenRequest(null), [])
  const opener = useMemo<AssistantOpener | null>(
    () =>
      shown
        ? {
            openSession: (sessionId) => {
              setOpenRequest({ sessionId })
              openPanel()
            },
          }
        : null,
    [shown, openPanel],
  )
  const closePanel = useCallback(() => {
    setOpen(false)
    // Back into a dialog left open beside it (#798), else to the toggle.
    const dialog = topmostDialog()
    if (dialog) focusInto(dialog)
    else toggleButton.current?.focus()
  }, [])
  // Full screen hides the panel along with the rest of the page, so there the toggle
  // means "show me the assistant": it leaves full screen and opens the panel, rather
  // than opening (or closing) it out of sight.
  const toggle = useCallback(() => {
    // A decision may just have landed in the panel or elsewhere.
    refreshAttention()
    const dialog = topmostDialog()
    // A dialog open beside the panel; one that covers it (aria-modal) does not count.
    const besideDialog = dialog?.getAttribute('aria-modal') === 'true' ? null : dialog
    if (leaveFullscreen()) {
      openPanel()
      // The browser's own full screen ends a moment later, and until it has, nothing
      // outside it can take the focus: give it to the panel again once it has.
      if (document.fullscreenElement) {
        document.addEventListener('fullscreenchange', openPanel, { once: true })
      }
    } else if (open && besideDialog) {
      // #798 — beside a dialog, the shortcut moves between the two rather than closing
      // the panel: the dialog stays open, and so does the chat about it.
      // Only beside it: a dialog that covers the panel (aria-modal) keeps the focus, and
      // the shortcut closes the panel as it always has.
      if (panelElement?.contains(document.activeElement)) focusInto(besideDialog)
      else setFocusKey((k) => k + 1)
    } else if (open) {
      closePanel()
    } else {
      openPanel()
    }
  }, [open, closePanel, openPanel, refreshAttention, panelElement])

  useEffect(() => {
    if (!shown) return
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || !isAssistantShortcut(event)) return
      event.preventDefault()
      toggle()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [shown, toggle])

  const onPanelKey = (event: ReactKeyboardEvent) => {
    if (event.key === 'Escape' && !event.defaultPrevented) {
      event.preventDefault()
      closePanel()
    }
  }

  return (
    <AssistantOpenerContext.Provider value={opener}>
      <ModalCompanionContext.Provider value={shown && mounted && open ? panelElement : null}>
        <div className="flex h-full min-h-0 flex-col bg-bg text-ink">
          <header
            className={`flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-line bg-surface px-4 py-1.5 sm:gap-x-5 ${
              embedded ? 'min-h-10' : 'min-h-14'
            }`}
            data-embedded={embedded ? 'true' : 'false'}
          >
            <NavLink to="/" className="flex items-baseline gap-1.5 shrink-0" aria-label="ScadBuddy">
              <span className={`font-semibold tracking-tight ${embedded ? 'text-[13px]' : 'text-base'}`}>
                Scad<span className="text-accent">Buddy</span>
              </span>
              {!embedded && (
                <span className="hidden text-[11px] text-faint sm:inline">OpenSCAD customizer</span>
              )}
            </NavLink>

            <nav className="flex items-center gap-0.5" aria-label="Main">
              {NAV.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    `rounded-[6px] px-2.5 py-1 text-[13px] transition-colors ${
                      isActive
                        ? 'bg-surface-3 text-ink'
                        : 'text-muted hover:bg-surface-2 hover:text-ink'
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>

            <div className="ml-auto flex items-center gap-2">
              <LiveUpdatesIndicator />
              {shown && (
                // Announces the badge (#815): it appears while focus is elsewhere, so the
                // button's own name changing is not enough. A bare live region, not
                // role=status, so it is not mistaken for a page's status message.
                <span data-testid="assistant-attention-live" aria-live="polite" className="sr-only">
                  {waitingLabel}
                </span>
              )}
              {shown && (
                <button
                  ref={toggleButton}
                  type="button"
                  onClick={toggle}
                  aria-expanded={open}
                  aria-controls={mounted ? PANEL_ID : undefined}
                  aria-keyshortcuts={ASSISTANT_SHORTCUT_ARIA}
                  aria-label={toggleLabel ? `Assistant, ${toggleLabel}` : undefined}
                  title={`Assistant (${ASSISTANT_SHORTCUT_LABEL})${toggleLabel ? `: ${[waitingLabel && `${waitingLabel} (${waitingDetail})`, summaries].filter(Boolean).join(', ')}` : ''}`}
                  className={`inline-flex items-center gap-1.5 rounded-[6px] px-2.5 py-1 text-[13px] transition-colors ${
                    open ? 'bg-surface-3 text-ink' : 'text-muted hover:bg-surface-2 hover:text-ink'
                  }`}
                >
                  Assistant
                  {attention.waiting !== null && attention.waiting > 0 && (
                    <span
                      data-testid="assistant-attention"
                      aria-hidden="true"
                      className="inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full border border-warn/50 bg-warn/10 px-1 text-[10.5px] leading-none font-semibold text-warn"
                    >
                      {attentionCount(attention.waiting)}
                    </span>
                  )}
                  {summaries && (
                    <span data-testid="assistant-summaries" aria-hidden="true" className="text-[11px] text-muted">
                      {summaries}
                    </span>
                  )}
                </button>
              )}
            </div>
          </header>

          {ai.available && (
            // #1002 — a link whose chunk failed costs only the browser bridge; the
            // boundary logs it to the console.
            <ErrorBoundary fallback={() => null}>
              <Suspense fallback={null}>
                <AgentLink factory={tabLink} />
              </Suspense>
            </ErrorBoundary>
          )}
          <div className="relative flex min-h-0 flex-1">
            <main className="min-h-0 min-w-0 flex-1 overflow-hidden">
              {/* #361 — the last resort: one page that throws never blanks the app, and
                  leaving it clears the error. Reload, not an in-place retry: what lands
                  here is mostly a lazy chunk that failed, which React caches as failed. */}
              <ErrorBoundary
                resetKey={location.pathname}
                fallback={() => <PageFailed />}
              >
                <Outlet />
              </ErrorBoundary>
            </main>
            {shown && mounted && (
              <aside
                ref={setPanelElement}
                id={PANEL_ID}
                aria-label="Assistant"
                hidden={!open}
                onKeyDown={onPanelKey}
                className="absolute inset-y-0 right-0 z-30 w-full max-w-[400px] border-l border-line bg-surface shadow-2xl md:static md:w-[380px] md:max-w-none md:shrink-0 md:shadow-none"
              >
                {/* #1002 — a panel that fails (mostly its chunk) stays in the panel. */}
                <ErrorBoundary fallback={() => <PanelFailed />}>
                  <Suspense fallback={<p className="p-3 text-[12.5px] text-muted">Loading the assistant…</p>}>
                    <AssistantPanel
                      onClose={closePanel}
                      focusKey={focusKey}
                      factory={assistantTransport}
                      embedded={embedded}
                      openRequest={openRequest}
                      onOpenHandled={openHandled}
                    />
                  </Suspense>
                </ErrorBoundary>
              </aside>
            )}
          </div>
        </div>
      </ModalCompanionContext.Provider>
    </AssistantOpenerContext.Provider>
  )
}

function PageFailed() {
  return (
    <div role="alert" className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-[13px]">
      <p className="text-ink">This page stopped working. The details are in the browser console.</p>
      <Button size="sm" onClick={() => window.location.reload()}>
        Reload page
      </Button>
    </div>
  )
}

function PanelFailed() {
  return (
    <div role="alert" className="flex flex-col items-start gap-3 p-3 text-[12.5px]">
      <p className="text-ink">The assistant failed to load. The details are in the browser console.</p>
      <Button size="sm" onClick={() => window.location.reload()}>
        Reload page
      </Button>
    </div>
  )
}
