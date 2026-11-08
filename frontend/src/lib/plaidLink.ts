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
export type PlaidGlobal = {
  create: (config: {
    token: string
    onSuccess: (publicToken: string, metadata: { accounts: LinkAccount[] }) => void
    onExit: (error: unknown) => void
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
    script.onload = () => (window.Plaid ? resolve(window.Plaid) : reject(new Error('Plaid Link did not load')))
    script.onerror = () => {
      loading = null
      script.remove()
      reject(new Error('Plaid Link did not load'))
    }
    document.head.appendChild(script)
  })
  return loading
}

/** What the shop chose in Link; null when they closed it. */
export type LinkChoice = { public_token: string; account_id: string }

export async function openPlaidLink(token: string): Promise<LinkChoice | null> {
  const plaid = await load()
  return new Promise((resolve, reject) => {
    const handler = plaid.create({
      token,
      onSuccess: (publicToken, metadata) => {
        handler.destroy()
        const [account, ...more] = metadata.accounts
        if (!account || more.length > 0) reject(new Error('Choose one checking or savings account in Plaid.'))
        else resolve({ public_token: publicToken, account_id: account.id })
      },
      onExit: () => {
        handler.destroy()
        resolve(null)
      },
    })
    handler.open()
  })
}
