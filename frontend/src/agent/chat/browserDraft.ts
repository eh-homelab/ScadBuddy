import type { SessionBrowserInput } from './protocol'

/**
 * #7027 — a new chat's browser options as the composer holds them (`NewChatBrowser`): the user
 * agent, viewport and locale the chat's browser (its Playwright MCP) reports. They go with the
 * chat's first message only, and the session keeps them for its life. Nothing here checks
 * bounds: the API decides, and the agent's `invalid_browser` refusal is the panel's notice.
 */
export interface BrowserDraft {
  userAgent: string
  width: string
  height: string
  locale: string
}

export const EMPTY_BROWSER: BrowserDraft = { userAgent: '', width: '', height: '', locale: '' }

export const PHONE_USER_AGENT =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
export const PHONE = { userAgent: PHONE_USER_AGENT, width: '390', height: '844' }

/** The options to send: only the keys set; undefined when none are. */
export function browserOptions(d: BrowserDraft): SessionBrowserInput | undefined {
  const userAgent = d.userAgent.trim()
  const locale = d.locale.trim()
  const width = d.width.trim()
  const height = d.height.trim()
  const out: SessionBrowserInput = {
    ...(userAgent ? { user_agent: userAgent } : {}),
    ...(width && height ? { viewport: { width: Number(width), height: Number(height) } } : {}),
    ...(locale ? { locale } : {}),
  }
  return Object.keys(out).length ? out : undefined
}

/** A draft the agent could not take as a viewport: one side set without the other. */
export function browserProblem(d: BrowserDraft): string | null {
  return !d.width.trim() !== !d.height.trim() ? 'Set both width and height, or neither.' : null
}

export type BrowserPreset = 'desktop' | 'phone' | null

/** The preset the draft matches; the locale is apart from both. */
export function presetOf(d: BrowserDraft): BrowserPreset {
  if (!d.userAgent.trim() && !d.width.trim() && !d.height.trim()) return 'desktop'
  if (d.userAgent.trim() === PHONE.userAgent && d.width.trim() === PHONE.width && d.height.trim() === PHONE.height) return 'phone'
  return null
}
