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
import { NavLink, Outlet, matchPath, useLocation } from 'react-router'
import {
  ActivityIcon,
  BoxIcon,
  BoxesIcon,
  LibraryBigIcon,
  PrinterIcon,
  SettingsIcon,
  SparklesIcon,
  WorkflowIcon,
} from 'lucide-react'
import {
  attentionCount,
  attentionDetail,
  attentionLabel,
  summaryLabel,
  useAttention,
  useAttentionTitle,
  WORKING_LABEL,
  RunsWaitingContext,
} from '../agent/attention'
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
import { panelWasOpen, rememberPanelOpen, rememberSession } from './assistant/panelMemory'
import { LiveUpdatesIndicator } from './LiveUpdatesIndicator'
import { ActivityBell } from './activity/ActivityBell'
import { Toasts } from './ui/Toasts'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  useSidebar,
} from '@/components/ui/sidebar'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { Spinner } from './ui/Spinner'
import { useLoadBambuddyLinks } from '../lib/bambuddyLinks'
import { useLoadDisplayUnit } from '../lib/units'
import { leaveFullscreen } from '../lib/useFullscreen'
import { ModalCompanionContext, focusInto, topmostDialog } from '../lib/modal'
import { useMediaQuery } from '../lib/useMediaQuery'
import { usePageTitle } from '../lib/usePageTitle'

// Split out: the panel, its protocol schemas (zod) and its renderer download only
// when someone opens it, and never when AI is off.
const AssistantPanel = lazy(async () => ({
  default: (await import('./assistant/AssistantPanel')).AssistantPanel,
}))

const NAV = [
  { to: '/', label: 'Models', end: true, icon: BoxesIcon },
  { to: '/prints', label: 'Prints', end: false, icon: PrinterIcon },
  { to: '/library', label: 'Library', end: false, icon: LibraryBigIcon },
  { to: '/workflows', label: 'Workflows', end: false, icon: WorkflowIcon },
  { to: '/activity', label: 'Activity', end: false, icon: ActivityIcon },
  { to: '/settings', label: 'Settings', end: false, icon: SettingsIcon },
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

/** Where the assistant panel lies over the page rather than beside it: below `md`. */
const PANEL_OVERLAYS_QUERY = '(max-width: 767.98px)'

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
  // #2372 — nor does it come back on the chat a reload would have restored.
  useEffect(() => {
    if (off) rememberSession(null)
  }, [off])
  // #815 — what waits on the user (approvals, questions, attention requests), shown on the
  // toggle so a closed panel (or a background session's, which never reaches this tab's
  // socket) still says so.
  const attention = useAttention(shown)
  const refreshAttention = attention.refresh
  const waitingLabel = attentionLabel(attention.waiting)
  const waitingDetail = attentionDetail(attention.counts)
  // A done summary waits for nothing, so it is shown beside the count, not in it.
  const summaries = summaryLabel(attention.counts)
  // #1125 — a turn under way in any of the user's sessions, shown whether or not the
  // panel is open. What waits on the user is the badge above, so this is the third
  // state: idle shows neither.
  const working = attention.running === true ? WORKING_LABEL : ''
  const toggleLabel = [working, waitingLabel, summaries].filter(Boolean).join(', ')
  const mainElement = useRef<HTMLElement>(null)
  useAttentionTitle(attention.waiting, !embedded, usePageTitle(mainElement))
  const [focusKey, setFocusKey] = useState(0)
  const toggleButton = useRef<HTMLButtonElement>(null)
  // #2013 — below `md` the panel lies over the page (the aside's classes), so a dialog
  // has no room beside it and covers it.
  const panelOverlays = useMediaQuery(PANEL_OVERLAYS_QUERY)

  // #2372 — a reload reopens the panel this tab had open, once the assistant is known to
  // be there, without taking the focus from the page. Remembered from then on.
  const reopen = useRef<boolean | null>(panelWasOpen())
  useEffect(() => {
    if (reopen.current === null || !ai.available) return
    if (reopen.current) {
      setMounted(true)
      setOpen(true)
    }
    reopen.current = null
  }, [ai.available])
  useEffect(() => {
    if (reopen.current === null) rememberPanelOpen(open)
  }, [open])

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
    } else if (dialog && !besideDialog && panelOverlays) {
      // #2013 — the dialog will still cover the panel: open it under the dialog, but
      // leave the focus in the dialog rather than in a composer no one can see.
      setMounted(true)
      setOpen(true)
    } else {
      openPanel()
    }
  }, [open, closePanel, openPanel, refreshAttention, panelElement, panelOverlays])

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
      <RunsWaitingContext.Provider value={attention.counts?.runs ?? null}>
      <ModalCompanionContext.Provider value={shown && mounted && open ? panelElement : null}>
      <TooltipProvider>
        {/* #3653 — shadcn's sidebar: beside the page from `md` up, collapsing to its icons;
            below `md` a sheet the header's trigger opens. */}
        <SidebarProvider defaultOpen={sidebarOpenAtStart(embedded)} className="h-full min-h-0 bg-background text-foreground">
          {/* #2454 — past the logo and the whole nav to the page, as the first Tab stop.
              Kept when embedded in Bambuddy's frame too: a keyboard user still tabs through
              the nav and the assistant's opener first. */}
          <a
            href="#main"
            onClick={(event) => {
              event.preventDefault()
              mainElement.current?.focus()
            }}
            className="sr-only z-50 rounded-md bg-popover px-3 py-2 text-sm text-popover-foreground focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:ring-2 focus:ring-ring"
          >
            Skip to main content
          </a>
          <AppSidebar embedded={embedded} />

          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            <header
              className={cn(
                'flex shrink-0 items-center gap-2 border-b bg-background px-2 sm:px-3',
                embedded ? 'min-h-10' : 'min-h-12',
              )}
              data-embedded={embedded ? 'true' : 'false'}
            >
              <Tooltip>
                <TooltipTrigger asChild>
                  <SidebarTrigger aria-label="Toggle navigation" />
                </TooltipTrigger>
                <TooltipContent side="bottom">Show or hide the navigation</TooltipContent>
              </Tooltip>
              <Separator orientation="vertical" className="my-auto data-[orientation=vertical]:h-4" />

              <div className="ml-auto flex items-center gap-1.5">
                <LiveUpdatesIndicator />
                <ActivityBell />
                {shown && (
                  // Announces the badge (#815): it appears while focus is elsewhere, so the
                  // button's own name changing is not enough. A bare live region, not
                  // role=status, so it is not mistaken for a page's status message.
                  <span data-testid="assistant-attention-live" aria-live="polite" className="sr-only">
                    {waitingLabel}
                  </span>
                )}
                {shown && (
                  // #1125 — its own region, so starting or finishing a turn does not
                  // announce the waiting count again.
                  <span data-testid="assistant-working-live" aria-live="polite" className="sr-only">
                    {working && 'The assistant is working'}
                  </span>
                )}
                {shown && (
                  <Button
                    ref={toggleButton}
                    variant="ghost"
                    size="sm"
                    onClick={toggle}
                    aria-expanded={open}
                    aria-controls={mounted ? PANEL_ID : undefined}
                    aria-keyshortcuts={ASSISTANT_SHORTCUT_ARIA}
                    aria-label={toggleLabel ? `Assistant, ${toggleLabel}` : undefined}
                    title={`Assistant (${ASSISTANT_SHORTCUT_LABEL})${toggleLabel ? `: ${[working, waitingLabel && `${waitingLabel} (${waitingDetail})`, summaries].filter(Boolean).join(', ')}` : ''}`}
                    // #1897 — above a dialog's overlay (ui/Dialog, z-50), so the assistant
                    // opens beside a dialog that is already open, as the shortcut does. Only
                    // where the panel can sit beside one (#2013): on a phone the dialog covers
                    // the panel anyway, and a raised button would paint over the dialog.
                    className={cn(!panelOverlays && 'relative z-[60]', open && 'bg-secondary text-secondary-foreground')}
                  >
                    {working ? (
                      <span
                        data-testid="assistant-working"
                        aria-hidden="true"
                        className="size-2 rounded-full bg-primary motion-safe:animate-pulse"
                      />
                    ) : (
                      <SparklesIcon aria-hidden="true" />
                    )}
                    Assistant
                    {attention.waiting !== null && attention.waiting > 0 && (
                      <Badge
                        data-testid="assistant-attention"
                        aria-hidden="true"
                        variant="destructive"
                        className="h-[18px] min-w-[18px] px-1 tabular-nums"
                      >
                        {attentionCount(attention.waiting)}
                      </Badge>
                    )}
                    {summaries && (
                      <span data-testid="assistant-summaries" aria-hidden="true" className="text-xs text-muted-foreground">
                        {summaries}
                      </span>
                    )}
                  </Button>
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
              <main
                id="main"
                ref={mainElement}
                tabIndex={-1}
                className="min-h-0 min-w-0 flex-1 overflow-hidden focus:outline-none"
              >
                {/* #361 — the last resort: one page that throws never blanks the app, and
                    leaving it clears the error. Reload, not an in-place retry: what lands
                    here is mostly a lazy chunk that failed, which React caches as failed. */}
                <ErrorBoundary
                  resetKey={location.pathname}
                  fallback={() => <PageFailed />}
                >
                  {/* #2356 — a page's own chunk loading. */}
                  <Suspense fallback={<PageLoading />}>
                    <Outlet />
                  </Suspense>
                </ErrorBoundary>
              </main>
              <Toasts />
              {shown && mounted && (
                <aside
                  ref={setPanelElement}
                  id={PANEL_ID}
                  aria-label="Assistant"
                  hidden={!open}
                  onKeyDown={onPanelKey}
                  className="absolute inset-y-0 right-0 z-30 w-full max-w-[400px] border-l bg-card text-card-foreground shadow-2xl md:static md:w-[380px] md:max-w-none md:shrink-0 md:shadow-none"
                >
                  {/* #1002 — a panel that fails (mostly its chunk) stays in the panel. */}
                  <ErrorBoundary fallback={() => <PanelFailed />}>
                    <Suspense fallback={<p className="p-3 text-sm text-muted-foreground">Loading the assistant…</p>}>
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
        </SidebarProvider>
      </TooltipProvider>
      </ModalCompanionContext.Provider>
      </RunsWaitingContext.Provider>
    </AssistantOpenerContext.Provider>
  )
}

/**
 * Whether the sidebar starts expanded: as the user last left it (shadcn's sidebar keeps
 * that in its `sidebar_state` cookie), else expanded on a wide window. Inside Bambuddy's
 * frame it starts as icons, beside Bambuddy's own navigation.
 */
function sidebarOpenAtStart(embedded: boolean): boolean {
  const saved = /(?:^|;\s*)sidebar_state=(true|false)/.exec(document.cookie)?.[1]
  if (saved) return saved === 'true'
  return !embedded && window.innerWidth >= WIDE_WINDOW
}

/** From here up the sidebar starts expanded; a page keeps 1024 px beside it. */
const WIDE_WINDOW = 1280

function AppSidebar({ embedded }: { embedded: boolean }) {
  const { pathname } = useLocation()
  const { isMobile, setOpenMobile } = useSidebar()
  // In the sheet a link's page replaces the sheet, as it does a dialog.
  const followed = () => {
    if (isMobile) setOpenMobile(false)
  }
  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton asChild size="lg" tooltip="ScadBuddy">
              <NavLink to="/" onClick={followed}>
                <span
                  aria-hidden="true"
                  className="flex aspect-square size-8 shrink-0 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground"
                >
                  <BoxIcon className="size-4" />
                </span>
                <span className="grid min-w-0 flex-1 text-left leading-tight">
                  <span className="truncate font-semibold tracking-tight">
                    Scad<span className="text-sidebar-primary">Buddy</span>
                  </span>
                  {/* #2454 — named by its own text, which the grid alone would run together. */}
                  {!embedded && ' '}
                  {!embedded && <span className="truncate text-xs text-muted-foreground">OpenSCAD customizer</span>}
                </span>
              </NavLink>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            <nav aria-label="Main">
              <SidebarMenu>
                {NAV.map((item) => (
                  <SidebarMenuItem key={item.to}>
                    <SidebarMenuButton
                      asChild
                      tooltip={item.label}
                      isActive={matchPath({ path: item.to, end: item.end }, pathname) !== null}
                    >
                      <NavLink to={item.to} end={item.end} onClick={followed}>
                        <item.icon aria-hidden="true" />
                        <span>{item.label}</span>
                      </NavLink>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </nav>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
    </Sidebar>
  )
}

function PageLoading() {
  return (
    <p role="status" className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
      <Spinner /> Loading
    </p>
  )
}

function PageFailed() {
  return (
    <div role="alert" className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-sm">
      <p>This page stopped working. The details are in the browser console.</p>
      <Button variant="outline" size="sm" onClick={() => window.location.reload()}>
        Reload page
      </Button>
    </div>
  )
}

function PanelFailed() {
  return (
    <div role="alert" className="flex flex-col items-start gap-3 p-3 text-sm">
      <p>The assistant failed to load. The details are in the browser console.</p>
      <Button variant="outline" size="sm" onClick={() => window.location.reload()}>
        Reload page
      </Button>
    </div>
  )
}
