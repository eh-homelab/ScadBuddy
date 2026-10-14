import type { GuardVerdict } from '../harness/permissions.js'
import {
  type ApprovedScope,
  type BrowserLists,
  compile,
  judge,
  type Judgement,
  pageUrls,
  refusalText,
  withheldText,
} from './origins.js'

// The headless browser's navigation policy as a turn applies it (#982), the same for a
// classic turn (harness/run.ts: the permission seam's input guard and a PostToolUse
// hook) and a durable one (temporal/turnServers.ts: the turn tools' guard and screen).
//
// The browser is any remote plugin's `browser_*` tools (Playwright MCP's names).
//   - check: a call that names a `url` (browser_navigate) is judged before it runs:
//     denied → refused at every tier, even with an approval; allowed → runs without
//     asking, whatever the tool's tier; on neither list → outward, so it parks for the
//     user, whatever the tool's tier.
//   - screen: a result is read for the page the browser is on afterwards (Playwright
//     MCP's `Page URL:` lines). A page the browser reached on its own (a redirect, a
//     link it followed, a new tab) that is denied, or on neither list and not approved
//     in the session, is withheld from the model: it reads a note saying where the
//     browser is instead. A navigation that ran after the user approved it once is
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

/** The page a browser call would open, when its input names one. */
export function navigationTarget(input: unknown): string | undefined {
  if (typeof input !== 'object' || input === null) return undefined
  const url = (input as { url?: unknown }).url
  return typeof url === 'string' ? url : undefined
}

/** Pages the browser shows that are no page at all, which carry nothing to withhold. */
function blank(url: string): boolean {
  return /^(about:(blank|srcdoc)|chrome-error:)/i.test(url)
}

export function browserGuard(deps: {
  store: BrowserOriginsAccess
  /** The session the turn is in; none for a flow run, which has no approvals to remember. */
  sessionId?: string | undefined
  /** The remote plugins' names in this turn (and their packages' http servers', which are forwarded the same way). */
  plugins: readonly string[]
}): BrowserGuard {
  // Read as each call is judged: a durable turn adds its packages' servers after building it.
  const covers = (harnessName: string) =>
    deps.plugins.some((name) => {
      const prefix = `mcp__${name}__`
      return harnessName.startsWith(prefix) && BROWSER_TOOL.test(harnessName.slice(prefix.length))
    })
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
      if (!covers(harnessName)) return undefined
      const url = navigationTarget(input)
      if (url === undefined) return undefined
      const { lists, approved } = await state()
      const j = judge(lists, approved, url, 'navigate')
      switch (j.kind) {
        case 'refused':
        case 'denied':
          return { deny: refusalText(j) }
        case 'allowed':
          return { allow: j.rule === undefined ? `${j.origin} was approved for this chat` : `${j.origin} is allowed by "${j.rule}"` }
        case 'ask':
          return {
            outward: `${harnessName} would open ${j.origin}, which is not on the headless browser's allow list, so it needs the user's approval in the ScadBuddy UI.`,
          }
      }
    },
    async screen(harnessName, input, text) {
      if (!covers(harnessName)) return undefined
      const pages = pageUrls(text).filter((u) => !blank(u))
      if (pages.length === 0) return undefined
      const { lists, approved } = await state()
      // The call's own navigation ran, so if it needed the user, the user approved it.
      const target = navigationTarget(input)
      const asked = target === undefined ? undefined : judge(lists, approved, target, 'navigate')
      if (asked?.kind === 'ask') {
        approved.set(asked.origin, approved.get(asked.origin) ?? 'once')
        if (deps.sessionId !== undefined) await deps.store.approve(deps.sessionId, asked.origin, 'once')
      }
      for (const page of pages) {
        const j: Judgement = judge(lists, approved, page, 'read')
        if (j.kind !== 'allowed') return withheldText(j)
      }
      return undefined
    },
  }
}
