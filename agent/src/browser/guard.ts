import type { GuardVerdict } from '../harness/permissions.js'
import {
  type ApprovedScope,
  blankTabText,
  type BrowserLists,
  compile,
  judge,
  type Judgement,
  openTabs,
  pageUrls,
  refusalText,
  tabWithheldText,
  unplacedText,
  withheldText,
} from './origins.js'

// The headless browser's navigation policy as a turn applies it (#982), the same for a
// classic turn (harness/run.ts: the permission seam's input guard and a PostToolUse
// hook) and a durable one (temporal/turnServers.ts: the turn tools' guard and screen).
//
// The browser is any remote plugin's `browser_*` tools (Playwright MCP's names).
//   - check: browser_navigate is judged by its `url` before it runs: denied (or no
//     URL at all) → refused at every tier, even with an approval; allowed → runs
//     without asking, whatever the tool's tier; on neither list → outward, so it parks
//     for the user. Any other browser tool that names a `url` can be refused or made
//     outward by it, never allowed: the allow list lowers only navigation's tier
//     (security review of #3654: a plugin's `browser_submit {url}` must not run
//     unasked because its URL is on the allow list).
//   - screen: a result is read for the page the browser is on afterwards (Playwright
//     MCP's `Page URL:` lines). A page the browser reached on its own (a redirect, a
//     link it followed, a new tab) that is denied, or on neither list and not approved
//     in the session, is withheld from the model: it reads a note saying where the
//     browser is instead. Each result is judged only by what it says itself: nothing
//     about the browser's page is remembered between calls, so an error, a timeout, a
//     tab switch, a popup or a history navigation cannot leave a stale page trusted.
//     Playwright MCP names the page only when the tool shows a snapshot or a tab
//     changed since its last result (measured in @playwright/mcp 0.0.82's
//     playwright-core, `Response._build`), so a result that names none
//     (browser_take_screenshot, browser_console_messages, browser_network_requests
//     after an unchanged page) is withheld, except from the tools that show no page
//     (NO_PAGE_TOOLS). Every tab in the tab list (`### Open tabs`, shown whenever more
//     than one is open) must be readable too, since the list shows each page's title;
//     a line that cannot be read unambiguously withholds the result. A blank tab
//     (about:blank) carries nothing of its own, but a page in another tab can write
//     into one it opened, so it is read only while it is the browser's one tab. A
//     navigation this guard sent to the user, which then ran, was approved: it is
//     recorded (`once`), so the pages of that origin may be read in the session.

/** What the guard needs of the store (store.ts BrowserOrigins). */
export type BrowserOriginsAccess = {
  lists(): Promise<BrowserLists>
  approved(sessionId: string): Promise<Map<string, ApprovedScope>>
  approve(sessionId: string, origin: string, scope: ApprovedScope): Promise<boolean>
}

export type BrowserGuard = {
  /** Whether `harnessName` (`mcp__<plugin>__<tool>`) is one of the browser's tools. */
  covers(harnessName: string): boolean
  /** The verdict on a call by its input; undefined when the input names no page. */
  check(harnessName: string, input: unknown): Promise<GuardVerdict | undefined>
  /** The text the model reads instead of `text`, or undefined to leave it as it is. */
  screen(harnessName: string, input: unknown, text: string): Promise<string | undefined>
}

const BROWSER_TOOL = /^browser_/
/** The one tool whose tier the allow list may lower. */
const NAVIGATE = 'browser_navigate'
/** Playwright MCP tools whose result carries no page content, so they need not name a page. */
const NO_PAGE_TOOLS: ReadonlySet<string> = new Set(['browser_close', 'browser_resize', 'browser_install'])

/** Whether `harnessName` is a browser tool's name, without its `mcp__<plugin>__` prefix. */
export function isBrowserTool(name: string): boolean {
  return BROWSER_TOOL.test(name)
}

/** The page a browser call would open, when its input names one. */
export function navigationTarget(input: unknown): string | undefined {
  if (typeof input !== 'object' || input === null) return undefined
  const url = (input as { url?: unknown }).url
  return typeof url === 'string' ? url : undefined
}

/** Chrome's own error page: no other page can script it, so it carries nothing to withhold. */
const CHROME_ERROR = 'chrome-error://chromewebdata/'
/** A tab with no page of its own, which the page that opened it can write into. */
function blank(url: string): boolean {
  return url === 'about:blank' || url === 'about:srcdoc'
}

export function browserGuard(deps: {
  store: BrowserOriginsAccess
  /** The session the turn is in; none for a flow run, which has no approvals to remember. */
  sessionId?: string | undefined
  /** The remote plugins' names in this turn (and their packages' http servers', which are forwarded the same way). */
  plugins: readonly string[]
}): BrowserGuard {
  // Read as each call is judged: a durable turn adds its packages' servers after building it.
  const toolOf = (harnessName: string): string | undefined => {
    for (const name of deps.plugins) {
      const prefix = `mcp__${name}__`
      if (harnessName.startsWith(prefix) && isBrowserTool(harnessName.slice(prefix.length))) return harnessName.slice(prefix.length)
    }
    return undefined
  }
  const covers = (harnessName: string) => toolOf(harnessName) !== undefined
  /** The origins this guard sent to the user: only a navigation to one of them that ran was approved. */
  const asked = new Set<string>()
  const state = async () => {
    const [lists, approved] = await Promise.all([
      deps.store.lists(),
      deps.sessionId === undefined ? Promise.resolve(new Map<string, ApprovedScope>()) : deps.store.approved(deps.sessionId),
    ])
    return { lists: compile(lists), approved }
  }
  return {
    covers,
    async check(harnessName, input) {
      const tool = toolOf(harnessName)
      if (tool === undefined) return undefined
      const url = navigationTarget(input)
      if (url === undefined) {
        // A navigation that names no URL is judged by nothing: refuse it rather than let it run at its tier.
        return tool === NAVIGATE ? { deny: `${harnessName} was not run: it names no url to judge.` } : undefined
      }
      // Exactly `{url}`: the approval card shows the one URL the call opens (frontend browserApproval.ts).
      if (tool === NAVIGATE && Object.keys(input as object).some((k) => k !== 'url')) {
        return { deny: `${harnessName} was not run: its input must be exactly {"url": "<the page>"}.` }
      }
      const { lists, approved } = await state()
      const j = judge(lists, approved, url, 'navigate')
      switch (j.kind) {
        case 'refused':
        case 'denied':
          return { deny: refusalText(j) }
        case 'allowed':
          if (tool !== NAVIGATE) return undefined
          return { allow: j.rule === undefined ? `${j.origin} was approved for this chat` : `${j.origin} is allowed by "${j.rule}"` }
        case 'ask':
          asked.add(j.origin)
          return {
            outward: `${harnessName} would open ${j.origin}, which is not on the headless browser's allow list, so it needs the user's approval in the ScadBuddy UI.`,
          }
      }
    },
    async screen(harnessName, input, text) {
      const tool = toolOf(harnessName)
      if (tool === undefined) return undefined
      const named = pageUrls(text)
      const tabs = openTabs(text)
      if (tabs === null) return tabWithheldText(undefined)
      if (named.length === 0 && tabs === undefined) return NO_PAGE_TOOLS.has(tool) ? undefined : unplacedText(harnessName)
      const { lists, approved } = await state()
      // The call's own navigation ran: if this guard sent it to the user, the user approved it.
      const target = navigationTarget(input)
      const judged = target === undefined ? undefined : judge(lists, approved, target, 'navigate')
      if (judged?.kind === 'ask' && asked.has(judged.origin)) {
        approved.set(judged.origin, approved.get(judged.origin) ?? 'once')
        if (deps.sessionId !== undefined) await deps.store.approve(deps.sessionId, judged.origin, 'once')
      }
      for (const page of named) {
        if (page === CHROME_ERROR) continue
        if (blank(page)) {
          if (tabs === undefined) continue
          return blankTabText(page)
        }
        const j: Judgement = judge(lists, approved, page, 'read')
        if (j.kind !== 'allowed') return withheldText(j)
      }
      for (const tab of tabs ?? []) {
        if (tab === CHROME_ERROR) continue
        if (blank(tab)) return blankTabText(tab)
        const j: Judgement = judge(lists, approved, tab, 'read')
        if (j.kind !== 'allowed') return tabWithheldText(j)
      }
      return undefined
    },
  }
}
