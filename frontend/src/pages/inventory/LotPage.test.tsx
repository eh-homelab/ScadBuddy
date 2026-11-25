import { screen } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { INVENTORY } from '../../mocks/features/inventory'
import { renderPage } from '../../test/utils'
import { LotPage } from './LotPage'

function Where() {
  const { pathname, hash } = useLocation()
  return <p data-testid="where">{pathname + hash}</p>
}

describe('LotPage (#7067)', () => {
  beforeEach(() => resetMockState())

  it("opens the lot's item at the lot's row", async () => {
    renderPage(
      <Routes>
        <Route path="/inventory/lots/:id" element={<LotPage />} />
        <Route path="*" element={<Where />} />
      </Routes>,
      { route: `/inventory/lots/${INVENTORY.HOOK_LOT}` },
    )
    expect(await screen.findByTestId('where')).toHaveTextContent(`/inventory/items/${INVENTORY.HOOK}#lot-${INVENTORY.HOOK_LOT}`)
  })

  it('says when there is no such lot', async () => {
    renderPage(
      <Routes>
        <Route path="/inventory/lots/:id" element={<LotPage />} />
      </Routes>,
      { route: '/inventory/lots/ab720000-0000-4000-8000-0000000000ff' },
    )
    expect(await screen.findByRole('alert')).toHaveTextContent('No such lot')
  })
})
