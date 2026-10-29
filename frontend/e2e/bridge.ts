import type { Page } from '@playwright/test'

/**
 * One call of the in-page agent's bridge (`window.__scadbuddyBridge`, `src/agent/index.ts`),
 * as the assistant makes it: the outcome, or the refusal (`code: 'refused'` for a user-only
 * control, `frontend/src/agent/dom.ts` `USER_ONLY`).
 */
export type Outcome = { ok: true; result: unknown } | { ok: false; error: { code: string; message: string } }

export async function bridgeCall(page: Page, name: string, args: unknown): Promise<Outcome> {
  return page.evaluate(
    ([tool, input]) =>
      (
        globalThis as unknown as { __scadbuddyBridge: { call: (n: string, a: unknown) => Promise<Outcome> } }
      ).__scadbuddyBridge.call(tool as string, input),
    [name, args] as const,
  )
}
