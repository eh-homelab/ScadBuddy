import type { GuardVerdict } from '../harness/permissions.js'
import {
  type ApprovedScope,
  blankTabText,
  type BrowserLists,
  compile,
  judge,
  type Judgement,
  pageUrls,
  refusalText,
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
//     browser is instead. Playwright MCP names the page only when the tool shows a
//     snapshot or a tab changed since its last result (measured in @playwright/mcp
//     0.0.82's playwright-core, `Response._build`), so a result that names none
//     (browser_take_screenshot, browser_console_messages, browser_network_requests) is
//     judged by the page this guard last saw that plugin's browser on, and withheld
//     when the guard has seen none (except from the tools that show no page,
//     NO_PAGE_TOOLS). A blank tab (about:blank) carries nothing of its own, but a page
//     in another tab can write into one it opened, so it is read only while it is the
//     browser's one tab (Playwright MCP lists the tabs, `### Open tabs`, whenever there
//     is more than one). A navigation this guard sent to the user, which then ran, was
//     approved: it is recorded (`once`), so the pages of that origin may be read in the
//     session.

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
/** The section Playwright MCP adds when more than one tab is open; page text can forge it, which only withholds more. */
const OPEN_TABS = '### Open tabs'

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

/** The page(s) one plugin's browser was last seen on, and whether that was its only tab. */
type Seen = { pages: string[]; alone: boolean }

export function browserGuard(deps: {
  store: BrowserOriginsAccess
  /** The session the turn is in; none for a flow run, which has no approvals to remember. */
  sessionId?: string | undefined
  /** The remote plugins' names in this turn (and their packages' http servers', which are forwarded the same way). */
  plugins: readonly string[]
}): BrowserGuard {
  // Read as each call is judged: a durable turn adds its packages' servers after building it.
  const browserOf = (harnessName: string): { plugin: string; tool: string } | undefined => {
    for (const name of deps.plugins) {
      const prefix = `mcp__${name}__`
      if (harnessName.startsWith(prefix) && isBrowserTool(harnessName.slice(prefix.length))) {
        return { plugin: name, tool: harnessName.slice(prefix.length) }
      }
    }
    return undefined
  }
  const toolOf = (harnessName: string): string | undefined => browserOf(harnessName)?.tool
  const covers = (harnessName: string) => toolOf(harnessName) !== undefined
  /** Each plugin's browser as its last result that named a page showed it. */
  const seen = new Map<string, Seen>()
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
      const browser = browserOf(harnessName)
      if (browser === undefined) return undefined
      const named = pageUrls(text)
      // Recorded before it is judged, so a later result that names no page is judged by this one too.
      if (named.length > 0) seen.set(browser.plugin, { pages: named, alone: !text.includes(OPEN_TABS) })
      else if (NO_PAGE_TOOLS.has(browser.tool)) return undefined
      const at = seen.get(browser.plugin)
      if (at === undefined) return unplacedText(harnessName)
      const { lists, approved } = await state()
      // The call's own navigation ran: if this guard sent it to the user, the user approved it.
      const target = navigationTarget(input)
      const judged = target === undefined ? undefined : judge(lists, approved, target, 'navigate')
      if (judged?.kind === 'ask' && asked.has(judged.origin)) {
        approved.set(judged.origin, approved.get(judged.origin) ?? 'once')
        if (deps.sessionId !== undefined) await deps.store.approve(deps.sessionId, judged.origin, 'once')
      }
      for (const page of at.pages) {
        if (page === CHROME_ERROR) continue
        if (blank(page)) {
          if (at.alone) continue
          return blankTabText(page)
        }
        const j: Judgement = judge(lists, approved, page, 'read')
        if (j.kind !== 'allowed') return withheldText(j)
      }
      return undefined
    },
  }
}
