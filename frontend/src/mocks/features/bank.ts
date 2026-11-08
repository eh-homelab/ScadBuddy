import { HttpResponse, http } from 'msw'
import type { BankKeys, BankLinkCreate, BankStatus } from '../../api/types'
import { now, problem } from './orders/state'

/**
 * #4961 — the shop's bank account linked through Plaid (`api/bank_link.py`). The mock keeps
 * no key and no token, only what Settings shows. Plaid's sandbox accounts: the checking
 * one (`mask` 0000) and the savings one (1111); any other account id is refused, as a
 * credit card is.
 */

const PROBLEMS = 'https://scadbuddy.dev/problems/payments/'

export const MOCK_LINK_TOKEN = 'link-sandbox-mock'
export const MOCK_CHECKING = 'mock-checking'
const ACCOUNTS: Record<string, NonNullable<BankStatus['account']>> = {
  [MOCK_CHECKING]: { name: 'Plaid Checking', type: 'checking', mask: '0000' },
  'mock-savings': { name: 'Plaid Saving', type: 'savings', mask: '1111' },
}

/** `BankUnlinked.note` when Plaid refused the keys (`REMOVE_AT_PLAID`). */
export const MOCK_REMOVE_AT_PLAID = 'Unlinked here, but Plaid refused the stored keys, so the Item is still at Plaid. Remove it in the Plaid Dashboard.'
let keysRefused = false
/** Plaid refuses the stored keys (a rotated secret): unlink drops the link here only. */
export function setMockKeysRefused(refused: boolean): void {
  keysRefused = refused
}

const EMPTY: BankStatus = { configured: false, environment: null, account: null, linked_at: null }
let bank: BankStatus = { ...EMPTY }

export function reset(): void {
  bank = { ...EMPTY }
  keysRefused = false
}

/** Saved keys and, with `linked`, the sandbox checking account, for a test's setup. */
export function mockBankLink({ linked }: { linked: boolean }): void {
  bank = { configured: true, environment: 'sandbox', account: linked ? ACCOUNTS[MOCK_CHECKING]! : null, linked_at: linked ? now() : null }
}

const linkedRefusal = (request: Request) => problem(request, 409, 'a bank account is already linked; unlink it first', PROBLEMS + 'bank_linked')
const notConfigured = (request: Request) => problem(request, 409, 'save the Plaid keys first', PROBLEMS + 'bank_not_configured')

export const handlers = [
  http.get('/api/v1/payments/bank', () => HttpResponse.json(bank)),
  http.put('/api/v1/payments/bank/keys', async ({ request }) => {
    if (bank.account) return linkedRefusal(request)
    const body = (await request.json()) as BankKeys
    if (body.secret === 'wrong') return problem(request, 422, 'Plaid refused: invalid client_id or secret provided')
    bank = { ...EMPTY, configured: true, environment: body.environment }
    return HttpResponse.json(bank)
  }),
  http.delete('/api/v1/payments/bank/keys', ({ request }) => {
    if (!bank.configured) return notConfigured(request)
    if (bank.account) return linkedRefusal(request)
    bank = { ...EMPTY }
    return new HttpResponse(null, { status: 204 })
  }),
  http.post('/api/v1/payments/bank/link-token', ({ request }) => {
    if (!bank.configured) return notConfigured(request)
    if (bank.account) return linkedRefusal(request)
    return HttpResponse.json({ link_token: MOCK_LINK_TOKEN })
  }),
  http.post('/api/v1/payments/bank/link', async ({ request }) => {
    if (!bank.configured) return notConfigured(request)
    if (bank.account) return linkedRefusal(request)
    const body = (await request.json()) as BankLinkCreate
    const account = ACCOUNTS[body.account_id]
    if (!account) return problem(request, 422, 'the chosen account is not a checking or savings account')
    bank = { ...bank, account, linked_at: now() }
    return HttpResponse.json(bank)
  }),
  http.delete('/api/v1/payments/bank/link', ({ request }) => {
    if (!bank.account) return problem(request, 409, 'no bank account is linked', PROBLEMS + 'bank_not_linked')
    bank = { ...bank, account: null, linked_at: null }
    return HttpResponse.json(keysRefused ? { removed_at_plaid: false, note: MOCK_REMOVE_AT_PLAID } : { removed_at_plaid: true, note: null })
  }),
]
