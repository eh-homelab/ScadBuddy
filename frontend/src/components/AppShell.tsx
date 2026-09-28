import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { NavLink, Outlet } from 'react-router'
import { useAiAvailability } from '../agent/chat/availability'
import {
  ASSISTANT_SHORTCUT_ARIA,
  ASSISTANT_SHORTCUT_LABEL,
  isAssistantShortcut,
} from '../agent/chat/shortcut'
import type { ChatTransportFactory } from '../agent/chat/transport'
import { useGlobalAgentTools } from '../agent/global'
import { isEmbedded } from '../lib/embed'
import { LiveUpdatesIndicator } from './LiveUpdatesIndicator'
import { useLoadDisplayUnit } from '../lib/units'

// Split out: the panel, its protocol schemas (zod) and its renderer download only
// when someone opens it, and never when AI is off.
const AssistantPanel = lazy(async () => ({
  default: (await import('./assistant/AssistantPanel')).AssistantPanel,
}))

const NAV = [
  { to: '/', label: 'Models', end: true },
  { to: '/settings', label: 'Settings', end: false },
]

const PANEL_ID = 'assistant-panel'

interface Props {
  embedded?: boolean
  /** Tests inject the mock agent; the app loads whichever this build has. */
  assistantTransport?: ChatTransportFactory
}

export function AppShell({ embedded = isEmbedded(), assistantTransport }: Props) {
  useLoadDisplayUnit()
  // #254 — navigate, snapshot and the click/fill fallbacks, on every route.
  useGlobalAgentTools()
  const ai = useAiAvailability()
  const [open, setOpen] = useState(false)
  // Mounted from the first open on, and hidden rather than unmounted when closed, so
  // closing the panel doesn't drop the connection or the transcript.
  const [mounted, setMounted] = useState(false)
  const [focusKey, setFocusKey] = useState(0)
  const toggleButton = useRef<HTMLButtonElement>(null)

  const openPanel = useCallback(() => {
    setMounted(true)
    setOpen(true)
    setFocusKey((k) => k + 1)
  }, [])
  const closePanel = useCallback(() => {
    setOpen(false)
    toggleButton.current?.focus()
  }, [])
  const toggle = useCallback(() => (open ? closePanel() : openPanel()), [open, closePanel, openPanel])

  useEffect(() => {
    if (!ai.available) return
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || !isAssistantShortcut(event)) return
      event.preventDefault()
      toggle()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [ai.available, toggle])

  const onPanelKey = (event: ReactKeyboardEvent) => {
    if (event.key === 'Escape' && !event.defaultPrevented) {
      event.preventDefault()
      closePanel()
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg text-ink">
      <header
        className={`flex shrink-0 items-center gap-5 border-b border-line bg-surface px-4 ${
          embedded ? 'h-10' : 'h-14'
        }`}
        data-embedded={embedded ? 'true' : 'false'}
      >
        <NavLink to="/" className="flex items-baseline gap-1.5 shrink-0" aria-label="ScadBuddy">
          <span className={`font-semibold tracking-tight ${embedded ? 'text-[13px]' : 'text-base'}`}>
            Scad<span className="text-accent">Buddy</span>
          </span>
          {!embedded && (
            <span className="text-[11px] text-faint">OpenSCAD customizer</span>
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
          {ai.available && (
          <button
            ref={toggleButton}
            type="button"
            onClick={toggle}
            aria-expanded={open}
            aria-controls={mounted ? PANEL_ID : undefined}
            aria-keyshortcuts={ASSISTANT_SHORTCUT_ARIA}
            title={`Assistant (${ASSISTANT_SHORTCUT_LABEL})`}
            className={`rounded-[6px] px-2.5 py-1 text-[13px] transition-colors ${
              open ? 'bg-surface-3 text-ink' : 'text-muted hover:bg-surface-2 hover:text-ink'
            }`}
          >
            Assistant
          </button>
          )}
        </div>
      </header>

      <div className="relative flex min-h-0 flex-1">
        <main className="min-h-0 min-w-0 flex-1 overflow-hidden">
          <Outlet />
        </main>
        {ai.available && mounted && (
          <aside
            id={PANEL_ID}
            aria-label="Assistant"
            hidden={!open}
            onKeyDown={onPanelKey}
            className="absolute inset-y-0 right-0 z-30 w-full max-w-[400px] border-l border-line bg-surface shadow-2xl md:static md:w-[380px] md:max-w-none md:shrink-0 md:shadow-none"
          >
            <Suspense fallback={<p className="p-3 text-[12.5px] text-muted">Loading the assistant…</p>}>
              <AssistantPanel onClose={closePanel} focusKey={focusKey} factory={assistantTransport} />
            </Suspense>
          </aside>
        )}
      </div>
    </div>
  )
}
