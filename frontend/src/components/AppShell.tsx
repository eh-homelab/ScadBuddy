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
import { NavLink, Outlet, matchPath, useLocation, useNavigate } from 'react-router'
import {
  ActivityIcon,
  BoxIcon,
  BoxesIcon,
  LibraryBigIcon,
  PrinterIcon,
  ReceiptIcon,
  SettingsIcon,
  SparklesIcon,
  SpoolIcon,
  StoreIcon,
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
  ApprovalsWaitingContext,
  type AttentionNotice,
  type PendingApproval,
  type WaitingEntry,
} from '../agent/attention'
import type { PairingEntry } from '../agent/linkProtocol'
import {
  NeedsYouContext,
  needsYouItems,
  needsYouLabel,
  needsYouNotices,
  notListed,
  useGrantRequests,
  useNeedsYouToasts,
  type NeedsYouSource,
  type NeedsYou,
} from '../lib/needsYou'
import { useAttentionNotifications } from '../agent/attentionNotifications'
import { useWebPush } from '../agent/webPush'
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
import { retryFailedPages } from '../lib/lazyRetry'
import { panelWasOpen, reloaded, rememberPanelOpen, rememberSession } from './assistant/panelMemory'
import { LiveUpdatesIndicator } from './LiveUpdatesIndicator'
import { ActivityBell } from './activity/ActivityBell'
import { AccountMenu } from './auth/AccountMenu'
import { SignInRequired } from './auth/SignInRequired'
import { useApprovalToasts } from '../agent/pendingApprovals'
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
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  useSidebar,
} from '@/components/ui/sidebar'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { Spinner } from './ui/spinner'
import { useLoadAppLinks } from '../lib/appLinks'
import { useLoadBambuddyLinks } from '../lib/bambuddyLinks'
import { useLoadDisplayUnit } from '../lib/loadDisplayUnit'
import { leaveFullscreen } from '../lib/useFullscreen'
import { ModalCompanionContext, escapeTaken, focusInto, topmostDialog } from '../lib/modal'
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
  { to: '/filament', label: 'Filament', end: false, icon: SpoolIcon },
  { to: '/orders', label: 'Orders', end: false, icon: ReceiptIcon },
  { to: '/storefront', label: 'Storefront', end: false, icon: StoreIcon },
  { to: '/workflows', label: 'Workflows', end: false, icon: WorkflowIcon },
  { to: '/activity', label: 'Activity', end: false, icon: ActivityIcon },
  { to: '/settings', label: 'Settings', end: false, icon: SettingsIcon },
]

// #254 — this tab's socket to the agent, and the pairing prompt, while the assistant is
// available; like the panel, loaded only then.
const AgentLink = lazy(async () => ({ default: (await import('./AgentLink')).AgentLink }))

const PANEL_ID = 'assistant-panel'
/** One empty list, so the approvals' context and toasts do not change while none waits. */
const NO_APPROVALS: readonly PendingApproval[] = []
const NO_NOTICES: readonly AttentionNotice[] = []
const NO_WAITING: readonly WaitingEntry[] = []
const NO_PAIRINGS: readonly PairingEntry[] = []

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
  // #3653 — read once; kept per browser in localStorage, which a cookie inside Bambuddy's
  // cross-origin frame would not be, and which no API request carries.
  const [sidebarOpen, setSidebarOpen] = useState(() => sidebarOpenAtStart(embedded))
  const changeSidebar = useCallback(
    (value: boolean) => {
      setSidebarOpen(value)
      rememberSidebar(embedded, value)
    },
    [embedded],
  )
  useLoadDisplayUnit()
  useLoadBambuddyLinks()
  useLoadAppLinks()
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
  // #4813 — the approvals no session's feed shows: listed in the panel and on Activity, and toasted.
  const unattached = attention.counts?.unattached ?? NO_APPROVALS
  const approvalsWaiting = useMemo(() => ({ approvals: unattached, refresh: refreshAttention }), [unattached, refreshAttention])
  useApprovalToasts(unattached, refreshAttention)
  const waitingLabel = attentionLabel(attention.waiting)
  const waitingDetail = attentionDetail(attention.counts)
  // A done summary waits for nothing, so it is shown beside the count, not in it.
  const summaries = summaryLabel(attention.counts)
  // #1125 — a turn under way in any of the user's sessions, shown whether or not the
  // panel is open. What waits on the user is the badge above, so this is the third
  // state: idle shows neither.
  const working = attention.running === true ? WORKING_LABEL : ''
  const toggleLabel = [working, waitingLabel, summaries].filter(Boolean).join(', ')
  // #6399 — everything that waits on the person, the assistant's and the rest: the flow grants
  // to decide, and the agents asking this tab to pair. The bell lists and counts it, Activity's
  // "Needs you" lists it in full, and each new one is toasted and (hidden) notified.
  const [pairings, setPairings] = useState<readonly PairingEntry[]>(NO_PAIRINGS)
  const { grants, refresh: refreshGrants } = useGrantRequests()
  const waitingEntries = attention.counts?.waiting ?? NO_WAITING
  // Built from whichever reads have answered, so a failing one hides none of the others; null
  // until one has (unknown is not "nothing waits"). The toasts skip what waited at a source's
  // first answer: a reload must not announce it again.
  const agentKnown = !shown || attention.counts !== null
  const grantsKnown = grants !== null
  const needsSources = useMemo(
    () => new Set<NeedsYouSource>(['pairings', ...(agentKnown ? (['agent'] as const) : []), ...(grantsKnown ? (['grants'] as const) : [])]),
    [agentKnown, grantsKnown],
  )
  const needsItems = useMemo(
    () => (agentKnown || grantsKnown ? needsYouItems(waitingEntries, grants, pairings) : null),
    [agentKnown, grantsKnown, waitingEntries, grants, pairings],
  )
  const refreshNeeds = useCallback(() => {
    refreshAttention()
    refreshGrants()
  }, [refreshAttention, refreshGrants])
  useNeedsYouToasts(needsItems, needsSources)
  const mainElement = useRef<HTMLElement>(null)
  useAttentionTitle(needsItems === null ? attention.waiting : needsItems.length, !embedded, usePageTitle(mainElement))
  const [focusKey, setFocusKey] = useState(0)
  const toggleButton = useRef<HTMLButtonElement>(null)
  // #2013 — below `md` the panel lies over the page (the aside's classes), so a dialog
  // has no room beside it and covers it.
  const panelOverlays = useMediaQuery(PANEL_OVERLAYS_QUERY)

  // #2372 — a reload reopens the panel this tab had open, once the assistant is known to
  // be there, without taking the focus from the page. Remembered from then on.
  const reopen = useRef<boolean | null>(panelWasOpen())
  // #3164 — read before the `[open]` effect below saves the panel as open: the panel is
  // a lazy chunk, so its chat first renders after that write and cannot read it itself.
  // Once per page load on purpose: a panel remounted later on the same page (the assistant
  // switched off and on) finds the chat forgotten (the `[off]` effect above), so it
  // opens on a new one however the page was loaded.
  const [wasReloaded] = useState(reloaded)
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
  // #815 — an OS notification for a pending request that opens while this page is hidden;
  // a click opens its session. Not when embedded: a cross-origin frame cannot ask for the permission.
  // #6399 — one set per source, each known once its own read answered, so what waited at a
  // source's first answer is never announced and a failing read holds up none of the others.
  const agentNotices = agentKnown ? (attention.counts?.notices ?? NO_NOTICES) : null
  const grantNotices = useMemo(
    () => (grantsKnown ? needsYouNotices(needsYouItems(NO_WAITING, grants, NO_PAIRINGS)) : null),
    [grantsKnown, grants],
  )
  const pairingNotices = useMemo(() => needsYouNotices(needsYouItems(NO_WAITING, null, pairings)), [pairings])
  const navigate = useNavigate()
  const openNotice = useCallback(
    (sessionId: string | null, href?: string) => {
      // #6399 — a request no session holds is answered on its page.
      if (href) return void navigate(href)
      if (sessionId) setOpenRequest({ sessionId })
      openPanel()
    },
    [openPanel, navigate],
  )
  useAttentionNotifications(agentNotices, !embedded, openNotice)
  useAttentionNotifications(grantNotices, !embedded, openNotice)
  useAttentionNotifications(pairingNotices, !embedded, openNotice)
  // #815 — and with every tab closed, through Web Push; its click opens the session here.
  useWebPush(shown && !embedded, openNotice)
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
  const needsYou = useMemo<NeedsYou>(
    () => ({
      items: needsItems,
      more: notListed(grants),
      refresh: refreshNeeds,
      openSession: shown
        ? (sessionId) => {
            if (sessionId) setOpenRequest({ sessionId })
            openPanel()
          }
        : null,
    }),
    [needsItems, grants, refreshNeeds, shown, openPanel],
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
    // Not `defaultPrevented`: a dialog beside the panel prevents the Escape it leaves to it.
    if (event.key === 'Escape' && !escapeTaken(event.nativeEvent)) {
      event.preventDefault()
      closePanel()
    }
  }

  return (
    <AssistantOpenerContext.Provider value={opener}>
      <RunsWaitingContext.Provider value={attention.counts?.runs ?? null}>
      <ApprovalsWaitingContext.Provider value={approvalsWaiting}>
      <NeedsYouContext.Provider value={needsYou}>
      <ModalCompanionContext.Provider value={shown && mounted && open ? panelElement : null}>
      <TooltipProvider>
        {/* #3653 — shadcn's sidebar: beside the page from `md` up, collapsing to its icons;
            below `md` a sheet the header's trigger opens. */}
        <SidebarProvider open={sidebarOpen} onOpenChange={changeSidebar} className="h-full min-h-0 bg-background text-foreground">
          {/* #2454 — past the logo and the whole nav to the page, as the first Tab stop.
              Kept when embedded in Bambuddy's frame too: a keyboard user still tabs through
              the nav and the assistant's opener first. */}
          <a
            href="#main"
            onClick={(event) => {
              event.preventDefault()
              // Focusable only while the skip link has sent focus there (#3393): a
              // permanent tabIndex would let any click on a bare spot of the page focus
              // <main>, leaving document.activeElement on it instead of the body.
              const main = mainElement.current
              // Already there: its blur listener is set, and another would only stack up.
              if (!main || document.activeElement === main) return
              main.tabIndex = -1
              main.addEventListener('blur', () => main.removeAttribute('tabindex'), { once: true })
              main.focus()
            }}
            className="sr-only z-50 rounded-md bg-popover px-3 py-2 text-sm text-popover-foreground focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:ring-2 focus:ring-ring"
          >
            Skip to main content
          </a>
          <AppSidebar embedded={embedded} needsYou={needsItems?.length ?? 0} />

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
                <AccountMenu />
                <SignInRequired />
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
                    // #1897 — above a dialog's overlay (ui/dialog, z-50), so the assistant
                    // opens beside a dialog that is already open, as the shortcut does. Only
                    // where the panel can sit beside one (#2013): on a phone the dialog covers
                    // the panel anyway, and a raised button would paint over the dialog.
                    // Clickable over a Radix modal too, which turns pointer events off outside it;
                    // the dialog leaves a press here alone (`data-modal-companion-toggle`).
                    data-modal-companion-toggle=""
                    className={cn(!panelOverlays && 'pointer-events-auto relative z-[60]', open && 'bg-secondary text-secondary-foreground')}
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
                  <AgentLink factory={tabLink} onPairings={setPairings} />
                </Suspense>
              </ErrorBoundary>
            )}
            <div className="relative flex min-h-0 flex-1">
              <main
                id="main"
                ref={mainElement}
                className="min-h-0 min-w-0 flex-1 overflow-hidden focus:outline-none"
              >
                {/* #361 — the last resort: one page that throws never blanks the app, and
                    leaving it clears the error. What lands here is mostly a lazy chunk that
                    failed; `retryFailedPages` drops that failure, so coming back to the
                    page imports it again (#3241). PageFailed's Reload reloads the whole page. */}
                <ErrorBoundary
                  resetKey={location.pathname}
                  fallback={() => <PageFailed />}
                  onError={retryFailedPages}
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
                        reloaded={wasReloaded}
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
      </NeedsYouContext.Provider>
      </ApprovalsWaitingContext.Provider>
      </RunsWaitingContext.Provider>
    </AssistantOpenerContext.Provider>
  )
}

/**
 * Where the sidebar's state is kept. One key inside Bambuddy's frame and another outside
 * it: they share the origin's storage, and the frame sits beside Bambuddy's own navigation.
 */
const sidebarKey = (embedded: boolean) => (embedded ? 'scadbuddy.sidebar.open.embedded' : 'scadbuddy.sidebar.open')

/**
 * Whether the sidebar starts expanded: as the user last left it in this browser, else
 * expanded on a wide window. Inside Bambuddy's frame it starts as icons, beside
 * Bambuddy's own navigation. Storage that throws (blocked site data) remembers nothing.
 */
function sidebarOpenAtStart(embedded: boolean): boolean {
  let saved: string | null = null
  try {
    saved = window.localStorage.getItem(sidebarKey(embedded))
  } catch {
    // Nothing remembered.
  }
  if (saved === 'true' || saved === 'false') return saved === 'true'
  return !embedded && window.innerWidth >= WIDE_WINDOW
}

function rememberSidebar(embedded: boolean, open: boolean): void {
  try {
    window.localStorage.setItem(sidebarKey(embedded), String(open))
  } catch {
    // Nothing is remembered; the sidebar still works for this page.
  }
}

/** From here up the sidebar starts expanded; a page keeps 1024 px beside it. */
const WIDE_WINDOW = 1280

function AppSidebar({ embedded, needsYou }: { embedded: boolean; needsYou: number }) {
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
                        {/* #6399 — named in the link, so it is read with it; the badge is only seen. */}
                        {item.to === '/activity' && needsYou > 0 && <span className="sr-only">, {needsYouLabel(needsYou)}</span>}
                      </NavLink>
                    </SidebarMenuButton>
                    {item.to === '/activity' && needsYou > 0 && (
                      <SidebarMenuBadge
                        aria-hidden="true"
                        data-testid="nav-needs-you"
                        className="rounded-full bg-needs font-semibold text-needs-ink peer-hover/menu-button:text-needs-ink peer-data-active/menu-button:text-needs-ink"
                      >
                        {needsYou}
                      </SidebarMenuBadge>
                    )}
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
