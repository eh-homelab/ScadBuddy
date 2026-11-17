import { screen, waitFor } from '@testing-library/react'
import { http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { Spool } from '../../api/filament'
import { server } from '../../mocks/server'
import { problem } from '../../mocks/handlers'
import { renderPage } from '../../test/utils'
import { BulkBar } from './BulkBar'

describe('the bulk bar', () => {
  it('does not double the full stop when a refusal already ends with one', async () => {
    server.use(http.post('/api/v1/filament/spools/s1/archive', () => problem(409, 'Conflict', 'The spool is on a printer.')))
    const onDone = vi.fn()
    const { user } = renderPage(<BulkBar selected={[{ id: 's1', number: 4 } as Spool]} places={[]} types={[]} onDone={onDone} onClear={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(onDone).toHaveBeenCalledWith('Archived 0 spools. #4 failed: The spool is on a printer.')
  })
})
