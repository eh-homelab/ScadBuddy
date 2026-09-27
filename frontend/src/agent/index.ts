import { bridge, type AgentBridge } from './bridge'
import { connectWebMcp } from './webmcp'

declare global {
  interface Window {
    /** Dev and mocked-e2e builds only; see `installAgentBridge`. */
    __scadbuddyBridge?: AgentBridge
  }
}

/**
 * Called once at startup.
 *
 * WebMCP is feature-detected in every build but registers nothing until the user opts
 * in on the Settings page (per browser; AI design spec §8.5).
 *
 * `window.__scadbuddyBridge` is for the dev server and the msw-mocked e2e build
 * (`VITE_MOCK_API=1`, `playwright.config.ts`) only. In a production bundle both
 * conditions are compile-time false, so the assignment is dropped: a production tab is
 * driven only through a paired session (AI design spec §8.5, #266), never by whatever
 * script happens to share the page.
 */
export function installAgentBridge() {
  connectWebMcp(bridge)
  if (import.meta.env.DEV || import.meta.env.VITE_MOCK_API === '1') {
    window.__scadbuddyBridge = bridge
  }
}
