import { useContext, useEffect, useMemo, useRef, type ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { useAiAvailability } from '../../agent/chat/availability'
import { McpAuthBanner } from '../../components/McpAuthBanner'
import { TipProvider } from '../../components/settings/form'
import { useLeaveGuard } from '../../lib/useLeaveGuard'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { SidebarMenuSub, SidebarMenuSubButton, SidebarMenuSubItem } from '@/components/ui/sidebar'
import { Skeleton } from '@/components/ui/skeleton'
import { SettingsAgentProvider } from './agentFields'
import { BambuddyResourcesProvider } from './bambuddyResources'
import { SettingsCacheProvider, useSettingsCache } from './cache'
import { SettingsCollapseProvider } from './collapse'
import { CollapseContext, useReveal } from './collapseState'
import { SettingsDirtyProvider, useDirtySections } from './dirty'
import { AREAS, foldIdsOf, resolveLocation, SECTION_REGISTRY, type SettingsSection } from './registry'
import { sectionId, useSectionNav } from './sectionNav'

/** Everything a Settings section reads, around the shell or a section rendered on its own. */
export function SettingsProviders({
  sections = SECTION_REGISTRY,
  children,
}: {
  sections?: readonly SettingsSection[]
  children: ReactNode
}) {
  const known = useMemo(() => foldIdsOf(sections), [sections])
  return (
    <SettingsCollapseProvider known={known}>
      <TipProvider>
        <SettingsDirtyProvider>
          <SettingsCacheProvider>
            <BambuddyResourcesProvider>
              <SettingsAgentProvider sections={sections}>{children}</SettingsAgentProvider>
            </BambuddyResourcesProvider>
          </SettingsCacheProvider>
        </SettingsDirtyProvider>
      </TipProvider>
    </SettingsCollapseProvider>
  )
}

/**
 * One section in its area. A `hideWhenEmpty` section whose body holds nothing (each of its
 * settings hid itself) is hidden, and so is its sub-nav link.
 */
function SectionSlot({ section, children }: { section: SettingsSection; children: ReactNode }) {
  const context = useContext(CollapseContext)
  const setEmpty = context?.setEmpty
  const slot = useRef<HTMLDivElement>(null)
  const { id, hideWhenEmpty } = section
  useEffect(() => {
    const element = slot.current
    if (!hideWhenEmpty || !element || !setEmpty) return
    // Its body may come and go with the section's own loading, so the slot is watched.
    const read = () => {
      const body = document.getElementById(`${id}-body`)
      setEmpty(id, !body || !element.contains(body) || body.childElementCount === 0)
    }
    read()
    const observer = new MutationObserver(read)
    observer.observe(element, { childList: true, subtree: true })
    return () => observer.disconnect()
  }, [id, hideWhenEmpty, setEmpty])
  return (
    <div ref={slot} hidden={hideWhenEmpty && context?.empty.has(id)}>
      {children}
    </div>
  )
}

/**
 * #2171 — Settings by area: `/settings/<area>`, each area's sections under one sub-nav.
 * Every area is rendered at once and the others hidden, so a draft and the agent's field
 * bindings survive a switch; the leave guard asks only when Settings itself is left.
 */
export function SettingsShell({ sections = SECTION_REGISTRY }: { sections?: readonly SettingsSection[] }) {
  const { pathname, hash } = useLocation()
  const navigate = useNavigate()
  const target = resolveLocation(pathname, hash, sections)
  const address = `/settings/${target.area}${target.hash}`
  /** The address is the one it resolves to: until then its hash may name an old anchor. */
  const resolved = `${pathname}${hash}` === address
  // Never an early return: the areas stay mounted through the redirect, so drafts survive.
  useEffect(() => {
    if (!resolved) void navigate(address, { replace: true })
  }, [resolved, address, navigate])

  const cache = useSettingsCache()
  const { settings } = cache
  const loaded = Boolean(settings)
  /** Loaded, at an address already redirected: what the hash names is a section here. */
  const ready = loaded && resolved
  const ai = useAiAvailability()
  // Not while its first read is out, nor when it answered unset up: Status & credentials says why.
  const down = ai.state === 'unavailable' || ai.state === 'unreachable'
  const collapse = useContext(CollapseContext)
  const dirtyIds = useDirtySections()
  const anyDirty = dirtyIds.length > 0
  const titleOf = (id: string) => sections.find((section) => section.id === id)?.title ?? id

  const shown = useMemo(
    () =>
      sections.filter(
        (section) => section.area === target.area && !(section.hideWhenEmpty && collapse?.empty.has(section.id)),
      ),
    [sections, target.area, collapse?.empty],
  )
  const shownIds = useMemo(() => shown.map((section) => section.id), [shown])

  const pane = useRef<HTMLDivElement>(null)
  const sectionNav = useRef<HTMLElement>(null)
  // #2173 — a section the address names is unfolded first, with any section around it;
  // useSectionNav then scrolls the pane to it.
  const reveal = useReveal()
  useEffect(() => {
    if (!ready || hash.length < 2) return
    reveal(sectionId(hash))
  }, [ready, hash, reveal])
  const { current: currentSection, go: goToSection } = useSectionNav(pane, sectionNav, shownIds, ready)

  // Another area opens at its top, unless the address names a section in it.
  const shownArea = useRef(target.area)
  useEffect(() => {
    if (shownArea.current === target.area) return
    shownArea.current = target.area
    const element = pane.current
    if (hash.length >= 2 || !element) return
    if (typeof element.scrollTo === 'function') element.scrollTo({ top: 0 })
    else element.scrollTop = 0
  }, [target.area, hash])

  const guard = useLeaveGuard(anyDirty, { within: '/settings' })

  // #1041 — a failed load is not "loading": say so, and offer the read again. Settings
  // is the page someone opens because something is wrong.
  if (cache.error && !settings) {
    return (
      <div role="alert" className="mx-auto max-w-lg px-4 py-16">
        <Card className="bg-warn/8 ring-warn/40">
          <CardContent>
            <p className="text-sm text-warn">Could not load settings: {cache.error.message}</p>
            <Button size="sm" variant="outline" className="mt-3" onClick={cache.reload}>
              Try again
            </Button>
          </CardContent>
        </Card>
      </div>
    )
  }

  // Only before the first answer: once the areas are mounted they hold drafts.
  if (!settings) {
    return (
      <div role="status" aria-busy="true" className="mx-auto max-w-2xl space-y-4 px-4 py-6">
        <span className="sr-only">Loading settings</span>
        <Skeleton className="h-6 w-32" />
        <Skeleton className="h-4 w-full" />
        {[0, 1, 2, 3].map((card) => (
          <Card key={card} className="gap-3">
            <CardContent className="space-y-3">
              <Skeleton className="h-4 w-1/3" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-2/3" />
            </CardContent>
          </Card>
        ))}
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col md:flex-row">
      <nav aria-label="Settings areas" className="shrink-0 border-b py-2 md:w-52 md:border-r md:border-b-0 md:py-6">
        {/* A column from md up; below it one sideways-scrolling strip, with a fade on the
            right edge to say there is more. One list, so each link exists once. */}
        <SidebarMenuSub className="mx-0 flex-row overflow-x-auto border-l-0 px-4 [mask-image:linear-gradient(to_right,black_85%,transparent)] md:mx-3.5 md:flex-col md:overflow-visible md:border-l md:px-2.5 md:[mask-image:none]">
          {AREAS.map((area) => {
            const current = area.id === target.area
            const dirty = sections.some((section) => section.area === area.id && dirtyIds.includes(section.id))
            return (
              <SidebarMenuSubItem key={area.id} className="shrink-0">
                <SidebarMenuSubButton asChild isActive={current}>
                  <Link to={`/settings/${area.id}`} aria-current={current ? 'page' : undefined}>
                    {area.title}
                    {dirty && <Badge aria-label="unsaved" className="size-1.5 min-w-0 rounded-full p-0" />}
                  </Link>
                </SidebarMenuSubButton>
              </SidebarMenuSubItem>
            )
          })}
        </SidebarMenuSub>
      </nav>

      {/* `relative`: the sr-only boxes inside are placed in the pane, not the document,
          which they otherwise made taller than the window, so the window scrolled (#2345). */}
      <div ref={pane} className="sb-settings relative h-full min-h-0 min-w-0 flex-1 overflow-y-auto">
        <nav
          ref={sectionNav}
          aria-label="Settings sections"
          className="sticky top-0 z-10 border-b bg-background/95 backdrop-blur"
        >
          {/* #969: wraps from sm up so every section shows at a glance; on a phone it
              scrolls sideways, with a fade on the right edge to say there is more. */}
          <ul className="mx-auto flex max-w-5xl gap-1 overflow-x-auto px-4 py-2 [mask-image:linear-gradient(to_right,black_85%,transparent)] sm:flex-wrap sm:justify-center sm:overflow-visible sm:[mask-image:none]">
            {shown.map((section) => (
              <li key={section.id} className="shrink-0">
                <a
                  href={`#${section.id}`}
                  aria-current={currentSection === section.id ? 'location' : undefined}
                  onClick={(event) => {
                    event.preventDefault()
                    reveal(section.id)
                    goToSection(section.id, { inPlace: anyDirty })
                  }}
                  // shadcn's tabs look (#3671): the section in view is the active tab. Links,
                  // not tabs: each moves to its section in this one page.
                  className="inline-flex h-7 items-center gap-1.5 rounded-md border border-transparent px-2.5 text-xs font-medium whitespace-nowrap text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none aria-[current=location]:border-border aria-[current=location]:bg-background aria-[current=location]:text-foreground aria-[current=location]:shadow-sm dark:aria-[current=location]:bg-input/30"
                >
                  {section.title}
                  {dirtyIds.includes(section.id) && (
                    <Badge aria-label="unsaved" className="size-1.5 min-w-0 rounded-full p-0" />
                  )}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <div className="mx-auto max-w-2xl px-4 py-6">
          <h1 className="font-heading text-xl font-semibold tracking-tight">Settings</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Each setting says where its value comes from. A value saved here beats the deployment&rsquo;s{' '}
            <span className="sb-num">SCADBUDDY_*</span> variable until it is reset. The API keys are stored on
            the server and never sent back to the browser.
          </p>

          {/* #1921 — while /mcp takes calls without a token; re-read when the mode is saved. */}
          {ai.available && <McpAuthBanner className="mt-4" />}

          {cache.changedElsewhere && (
            <Card role="status" size="sm" className="mt-4 bg-primary/8 ring-primary/40">
              <CardContent className="flex flex-wrap items-center gap-3 text-xs">
                <span className="min-w-0 flex-1 basis-60">
                  Settings were changed elsewhere. Your unsaved changes here are kept until you load them.
                </span>
                <Button size="sm" onClick={cache.loadLatest}>
                  Load the latest
                </Button>
                <Button size="sm" variant="ghost" onClick={cache.keepMine}>
                  Keep mine
                </Button>
              </CardContent>
            </Card>
          )}

          <div className="mt-5">
            {AREAS.map((area) => (
              <div key={area.id} data-area={area.id} hidden={area.id !== target.area} className="space-y-4">
                {/* Announces the area on a switch; the page's h1 stays "Settings". */}
                <h2 className="sr-only">{area.title}</h2>
                {area.servedByAgent && down && (
                  <p role="status" className="text-sm text-muted-foreground">
                    The assistant service is not answering: the settings it serves are hidden until it does.
                  </p>
                )}
                {sections
                  .filter((section) => section.area === area.id)
                  .map((section) => (
                    <SectionSlot key={section.id} section={section}>
                      <section.Component />
                    </SectionSlot>
                  ))}
              </div>
            ))}
          </div>
        </div>
      </div>

      <AlertDialog open={guard.pending !== null} onOpenChange={(open) => !open && guard.stay()}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Leave without saving?</AlertDialogTitle>
            <AlertDialogDescription>
              {`Unsaved changes in ${dirtyIds.map(titleOf).join(', ')} will be lost.`} Save each section first to
              keep its changes.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Stay</AlertDialogCancel>
            {/* A plain button: an AlertDialogAction would also close the dialog, which is Stay. */}
            <Button variant="destructive" onClick={guard.leave}>
              Leave without saving
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
