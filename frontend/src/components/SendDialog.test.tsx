import { trace } from '@opentelemetry/api'
import { screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { outputs } from '../mocks/fixtures'
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
