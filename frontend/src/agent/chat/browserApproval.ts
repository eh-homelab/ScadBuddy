/**
 * #982 — an approval the headless browser asked for: a navigation to a site on neither of
 * its lists. Its card offers "Allow once", "Allow for this chat" and "Always allow". The
 * call is a remote plugin's `browser_navigate` (Playwright MCP; a classic turn names it
 * `mcp__<plugin>__browser_navigate`, a durable one `<plugin>__browser_navigate`), never
 * the browser bridge's own `mcp__scadbuddy__browser_navigate`, which drives the user's tab.
 * Both are read from the card's summary, which is the tool's name, a space, and its
 * input as JSON (agent `approvals/service.ts`, agent-durable `gate/store.py`); the
 * card's `tool` is the call's id.
 */
export type BrowserNavigation = { url: string; origin: string }

export function browserNavigation(summary: string): BrowserNavigation | undefined {
  const space = summary.indexOf(' ')
  if (space < 0) return undefined
  const tool = summary.slice(0, space)
  if (!/(?:^|__)browser_navigate$/.test(tool) || /(?:^|__)scadbuddy__browser_navigate$/.test(tool)) return undefined
  // The whole input, parsed, and exactly `{url}`: the top-level `url` is what the agent's
  // guard judged and the browser is sent to, so a nested one, an extra key, or a summary
  // the agent cut short (APPROVAL_SUMMARY_MAX) leaves the plain card showing the summary.
  let input: unknown
  try {
    input = JSON.parse(summary.slice(space + 1))
  } catch {
    return undefined
  }
  if (typeof input !== 'object' || input === null || Array.isArray(input)) return undefined
  const keys = Object.keys(input)
  const raw = (input as { url?: unknown }).url
  if (keys.length !== 1 || keys[0] !== 'url' || typeof raw !== 'string') return undefined
  try {
    const url = new URL(raw)
    // The guard asks only about a URL written as the browser writes it; any other never parks.
    if (url.href !== raw || (url.protocol !== 'https:' && url.protocol !== 'http:')) return undefined
    return { url: url.href, origin: url.origin }
  } catch {
    return undefined
  }
}
