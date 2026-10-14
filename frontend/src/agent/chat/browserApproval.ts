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
  const tool = summary.split(' ', 1)[0] ?? ''
  if (!/(?:^|__)browser_navigate$/.test(tool) || /(?:^|__)scadbuddy__browser_navigate$/.test(tool)) return undefined
  const match = /"url"\s*:\s*("(?:[^"\\]|\\.)*")/.exec(summary)
  if (!match) return undefined
  try {
    const url = new URL(JSON.parse(match[1]!) as string)
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return undefined
    return { url: url.href, origin: `${url.protocol}//${url.hostname.replace(/\.$/, '')}${url.port ? `:${url.port}` : ''}` }
  } catch {
    return undefined
  }
}
