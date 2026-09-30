/**
 * This tab's id for the browser bridge (#254): 128 random bits, base64url, made once per
 * page load. The tab's socket to the agent says it in its `hello` (`link.ts`), and the
 * assistant panel says it on its own socket (`tab.bind`), so the agent knows that the
 * sessions chatted with from here drive this tab (agent `src/bridge/hub.ts`).
 *
 * In memory only, never in storage: `sessionStorage` would survive a reload but is copied
 * into a duplicated tab, which would then answer for the original, and storage throws in
 * some sandboxed frames. So a reload is a new tab, and an agent paired by code pairs again
 * (agent `src/bridge/pairings.ts`). `crypto.getRandomValues`, not `randomUUID`, because
 * the latter needs a secure context and the Bambuddy iframe may be served without one.
 */
function newTabId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export const TAB_ID = newTabId()
