import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { emitRealtime } from '../mocks/realtime'
import { getRealtime, resetRealtime, useLiveQuery } from './realtime'

// The real WebSocket, intercepted by msw: the same path the app takes in the mocked e2e run.

afterEach(() => resetRealtime())

function Count({ reads }: { reads: () => Promise<number> }) {
  const { data } = useLiveQuery('count', reads, ['models'])
  return <p>reads: {data ?? '…'}</p>
}

it('re-reads when an event for a followed topic arrives over the socket', async () => {
  let reads = 0
  render(<Count reads={() => Promise.resolve(++reads)} />)
  // Once on mount, once when the server confirms the subscription.
  await waitFor(() => expect(getRealtime().status).toBe('live'))
  await waitFor(() => expect(screen.getByText('reads: 2')).toBeInTheDocument())
  emitRealtime('model.created', ['models'], { slug: 'new' })
  await screen.findByText('reads: 3')
  emitRealtime('font.installed', ['fonts'], { family: 'x' })
  await new Promise((resolve) => setTimeout(resolve, 50))
  expect(reads).toBe(3)
})
