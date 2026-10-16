import { render, screen } from '@testing-library/react'
import { Suspense } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { lazyRetry, retryFailedPages } from './lazyRetry'

function Page() {
  return <h1>the page</h1>
}

describe('lazyRetry (#3241)', () => {
  it('imports again on the next visit after a rejected import, not replaying its error', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const load = vi
      .fn<() => Promise<{ default: typeof Page }>>()
      .mockRejectedValueOnce(new TypeError('Failed to fetch dynamically imported module'))
      .mockResolvedValue({ default: Page })
    const Lazy = lazyRetry(load)
    const visit = () =>
      render(
        <ErrorBoundary fallback={() => <p>page failed</p>} onError={retryFailedPages}>
          <Suspense fallback={<p>loading</p>}>
            <Lazy />
          </Suspense>
        </ErrorBoundary>,
      )

    const first = visit()
    expect(await screen.findByText('page failed')).toBeInTheDocument()
    // Shown, not retried within the same visit.
    expect(load).toHaveBeenCalledTimes(1)
    first.unmount()

    // Back to the route: the import runs again, and this time the page renders.
    visit()
    expect(await screen.findByRole('heading', { name: 'the page' })).toBeInTheDocument()
    expect(load).toHaveBeenCalledTimes(2)
    vi.restoreAllMocks()
  })
})
