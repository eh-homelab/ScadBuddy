/**
 * #4961 — Plaid Link in the page. Plaid says the script "must always be loaded directly
 * from `https://cdn.plaid.com`, rather than included in a bundle or hosted yourself", and
 * `Plaid.create({token, onSuccess, onExit})` opens it; `onSuccess` gets the public token
 * and `metadata.accounts`, each with its `id` (https://plaid.com/docs/link/web/). PAGE_CSP
 * allows this one script and its frame (`backend/scadbuddy/api/static.py`).
 */

export const PLAID_LINK_SCRIPT = 'https://cdn.plaid.com/link/v2/stable/link-initialize.js'

type LinkAccount = { id: string }
type LinkHandler = { open: () => void; destroy: () => void }
/** The `error` `onExit` gets when Link failed; null when the shop closed it
 * (https://plaid.com/docs/link/web/#onexit). */
export type PlaidLinkError = { display_message?: string | null; error_message?: string | null }
export type PlaidGlobal = {
  create: (config: {
    token: string
    onSuccess: (publicToken: string, metadata: { accounts: LinkAccount[] }) => void
    onExit: (error: PlaidLinkError | null) => void
  }) => LinkHandler
}

declare global {
  interface Window {
    Plaid?: PlaidGlobal
  }
}

let loading: Promise<PlaidGlobal> | null = null

function load(): Promise<PlaidGlobal> {
  if (window.Plaid) return Promise.resolve(window.Plaid)
  loading ??= new Promise<PlaidGlobal>((resolve, reject) => {
    const script = document.createElement('script')
    script.src = PLAID_LINK_SCRIPT
    script.async = true
    // A failed load, or a script that defined no `Plaid`, can be tried again.
    const fail = () => {
      loading = null
      script.remove()
      reject(new Error('Plaid Link did not load'))
    }
    script.onload = () => (window.Plaid ? resolve(window.Plaid) : fail())
    script.onerror = fail
    document.head.appendChild(script)
  })
  return loading
}

/** What the shop chose in Link; null when they closed it. Every chosen id is sent, even
 * none or several: the API exchanges the token and removes the Item it will not keep. */
export type LinkChoice = { public_token: string; account_ids: string[] }

export async function openPlaidLink(token: string): Promise<LinkChoice | null> {
  const plaid = await load()
  return new Promise((resolve, reject) => {
    const handler = plaid.create({
      token,
      onSuccess: (publicToken, metadata) => {
        handler.destroy()
        resolve({ public_token: publicToken, account_ids: metadata.accounts.map((a) => a.id) })
      },
      onExit: (error) => {
        handler.destroy()
        const message = error?.display_message || error?.error_message
        if (message) reject(new Error(message))
        else resolve(null)
      },
    })
    handler.open()
  })
}
