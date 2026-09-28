import { act, render, screen } from '@testing-library/react'
import { ws } from 'msw'
import { afterEach, expect, it, vi } from 'vitest'
import { getRealtime, resetRealtime } from '../lib/realtime'
import { server } from '../mocks/server'
import { LiveUpdatesIndicator } from './LiveUpdatesIndicator'

afterEach(() => {
  vi.useRealTimers()
  resetRealtime()
})

it('says nothing while the socket is live', async () => {
  render(<LiveUpdatesIndicator />)
  const stop = getRealtime().subscribe('models', () => {})
  await vi.waitFor(() => expect(getRealtime().status).toBe('live'))
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
  stop()
})

it('shows while something is followed and no socket connects, and hides when nothing is', () => {
  // A server that refuses every connection.
  server.use(
    ws.link('*/api/v1/ws').addEventListener('connection', ({ client }) => client.close(1008)),
  )
  vi.useFakeTimers()
  render(<LiveUpdatesIndicator />)
  let stop = () => {}
  act(() => {
    stop = getRealtime().subscribe('models', () => {})
  })
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
  act(() => vi.advanceTimersByTime(5_000))
  expect(screen.getByRole('status')).toHaveTextContent('Live updates unavailable')
  act(() => stop())
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
})
