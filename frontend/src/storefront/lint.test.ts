import { ESLint } from 'eslint'
import { describe, expect, it } from 'vitest'

// Spec 2026-11-02 §5.1: the public page imports nothing from the back office, and the
// lint rule in eslint.config.js enforces it.

async function restricted(code: string, filePath: string) {
  const eslint = new ESLint({ cwd: process.cwd() })
  const [result] = await eslint.lintText(code, { filePath })
  return result!.messages.filter((m) => m.ruleId === 'no-restricted-imports')
}

describe('the storefront lint rule', () => {
  it('refuses a back-office import from the storefront', async () => {
    const eslint = new ESLint({ cwd: process.cwd() })
    const [bad] = await eslint.lintText("import { OrdersPage } from '../pages/OrdersPage'\n", { filePath: 'src/storefront/App.tsx' })
    expect(bad!.messages.map((m) => m.ruleId)).toContain('no-restricted-imports')
    const [ok] = await eslint.lintText("import { formatMoney } from '../lib/money'\n", { filePath: 'src/storefront/App.tsx' })
    expect(ok!.messages.filter((m) => m.ruleId === 'no-restricted-imports')).toEqual([])
  })

  it('names the reason', async () => {
    const [message] = await restricted("import { api } from '../api/client'\n", 'src/storefront/App.tsx')
    expect(message!.message).toContain('The storefront imports nothing from the back office (spec §5.1)')
  })

  it.each([
    ["import { api } from '../../api/client'\n", 'src/storefront/pages/DraftPage.tsx'],
    ["import { X } from '../../components/orders/OrderView'\n", 'src/storefront/pages/DraftPage.tsx'],
    ["import { X } from '../components/settings/Printers'\n", 'src/storefront/App.tsx'],
    ["import { X } from '../agent/useFillable'\n", 'src/storefront/App.tsx'],
    ["import { X } from '../../pages/CustomizePage'\n", 'src/storefront/components/Estimate.tsx'],
    // Any back-office API module, not only the client: one could pull the client in.
    ["import { X } from '../api/schema'\n", 'src/storefront/App.tsx'],
    ["import { X } from '../../api/realtime'\n", 'src/storefront/pages/DraftPage.tsx'],
  ])('refuses %s', async (code, filePath) => {
    expect(await restricted(code, filePath)).not.toEqual([])
  })

  it.each([
    ["import { ListingsPage } from './pages/ListingsPage'\n", 'src/storefront/App.tsx'],
    ["import { store } from './api/client'\n", 'src/storefront/App.tsx'],
    ["import { store } from '../api/client'\n", 'src/storefront/pages/DraftPage.tsx'],
    ["import { Preview } from '../../components/Preview'\n", 'src/storefront/pages/CustomizePage.tsx'],
    ["import { ParamWidget } from '../../components/widgets/ParamWidget'\n", 'src/storefront/pages/CustomizePage.tsx'],
    ["import type { components } from './schema'\n", 'src/storefront/api/client.ts'],
    ["import type { Listing } from '../api/client'\n", 'src/storefront/components/ListingView.tsx'],
    ["import type { Job } from '../../api/types'\n", 'src/storefront/pages/CustomizePage.tsx'],
    ["import type { Job } from '../api/types'\n", 'src/storefront/App.tsx'],
  ])('allows %s', async (code, filePath) => {
    expect(await restricted(code, filePath)).toEqual([])
  })
})
