import { act, render } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { useHashTarget } from './useHashTarget'

function Rows() {
  useHashTarget(true)
  return <div id="row-1">row</div>
}

// jsdom has no scrollIntoView; define it once so a spy can wrap it and be restored.
if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {}

afterEach(() => {
  vi.restoreAllMocks()
})

it('scrolls to the same anchor again on a second visit to it', () => {
  const scroll = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
  const router = createMemoryRouter([{ path: '/sheet', element: <Rows /> }], { initialEntries: ['/sheet#row-1'] })
  render(<RouterProvider router={router} />)
  expect(scroll).toHaveBeenCalledTimes(1)
  act(() => {
    void router.navigate('/sheet#row-1')
  })
  expect(scroll).toHaveBeenCalledTimes(2)
})

it('scrolls once per visit, not on every render', () => {
  const scroll = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
  const router = createMemoryRouter([{ path: '/sheet', element: <Rows /> }], { initialEntries: ['/sheet#row-1'] })
  const { rerender } = render(<RouterProvider router={router} />)
  rerender(<RouterProvider router={router} />)
  expect(scroll).toHaveBeenCalledTimes(1)
})
