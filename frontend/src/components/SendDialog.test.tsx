import { trace } from '@opentelemetry/api'
import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { outputs } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { installTestTracing } from '../test/tracing'
import { renderPage } from '../test/utils'
import { SendDialog } from './SendDialog'

describe('SendDialog, traced', () => {
  afterEach(() => vi.restoreAllMocks())

  it('records Send as a send span naming the output, with the send request inside it', async () => {
    const tracing = installTestTracing()
    try {
      const output = outputs[0]!
      let active: string | undefined
      const sendOutput = api.sendOutput
      vi.spyOn(api, 'sendOutput').mockImplementation((id, body) => {
        active = trace.getActiveSpan()?.spanContext().spanId
        return sendOutput(id, body)
      })
      const onSent = vi.fn()
      const { user } = renderPage(<SendDialog open output={output} onClose={() => {}} onSent={onSent} />)
      await user.click(screen.getByRole('button', { name: 'Send' }))
      await waitFor(() => expect(onSent).toHaveBeenCalled())

      const [span] = tracing.exporter.getFinishedSpans()
      expect(span?.name).toBe('send')
      expect(span?.attributes).toEqual({ 'scadbuddy.output_id': output.id })
      expect(active).toBe(span?.spanContext().spanId)
    } finally {
      tracing.uninstall()
    }
  })
})

describe('SendDialog · plates (#986)', () => {
  it('names the plates being sent, an unnamed one by its number', async () => {
    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: true, name: 'Lid' },
          { index: 2, has_thumbnail: true, name: null },
        ]),
      ),
    )
    renderPage(<SendDialog open output={outputs[0]!} onClose={() => {}} onSent={() => {}} />)
    expect(await screen.findByTestId('send-plates')).toHaveTextContent('Plates: Lid · Plate 2')
  })

  it('says nothing about plates when none is named', async () => {
    let read = false
    server.use(
      http.get('/api/v1/outputs/:id/plates', () => {
        read = true
        return HttpResponse.json([{ index: 1, has_thumbnail: true }])
      }),
    )
    renderPage(<SendDialog open output={outputs[0]!} onClose={() => {}} onSent={() => {}} />)
    await waitFor(() => expect(read).toBe(true))
    await screen.findByRole('button', { name: 'Send' })
    expect(screen.queryByTestId('send-plates')).not.toBeInTheDocument()
  })
})
