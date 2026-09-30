import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { AiStatusSection } from './AiStatusSection'

describe('AiStatusSection', () => {
  it('shows the assistant ready when the agent says so', async () => {
    render(<AiStatusSection />)
    await waitFor(() => expect(screen.getByTestId('ai-status')).toHaveAttribute('data-state', 'configured'))
    expect(screen.getByTestId('ai-status')).toHaveTextContent('Ready')
  })

  it('names what is missing, and checks again on request', async () => {
    server.use(
      http.get('/api/v1/ai/status', () =>
        HttpResponse.json({
          available: false,
          state: 'disabled',
          ai: 'disabled (no Claude credential)',
          reason: 'No Claude credential is configured yet.',
        }),
      ),
    )
    render(<AiStatusSection />)
    await waitFor(() => expect(screen.getByTestId('ai-status')).toHaveAttribute('data-state', 'not_configured'))
    expect(screen.getByTestId('ai-status')).toHaveTextContent('Not set up — No Claude credential is configured yet.')

    server.resetHandlers()
    await userEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(screen.getByTestId('ai-status')).toHaveAttribute('data-state', 'configured'))
  })

  it('says the agent is unreachable when the route answers with the app page', async () => {
    server.use(
      http.get(
        '/api/v1/ai/status',
        () => new HttpResponse('<!doctype html>', { headers: { 'content-type': 'text/html' } }),
      ),
    )
    render(<AiStatusSection />)
    await waitFor(() => expect(screen.getByTestId('ai-status')).toHaveAttribute('data-state', 'unreachable'))
    expect(screen.getByTestId('ai-status')).toHaveTextContent(/ingress routes \/api\/v1\/ai/)
  })
})
