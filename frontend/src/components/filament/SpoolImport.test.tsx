import { screen, waitFor, within } from '@testing-library/react'
import { delay, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { SpoolImport } from './SpoolImport'

const IMPORT = '/api/v1/filament/spools/import'

/** Every import request, its body and key, before the mock answers it. */
function watchImports() {
  const sent: { dry_run: boolean; rows: unknown[]; key: string | null }[] = []
  server.use(
    http.post(IMPORT, async ({ request }) => {
      const body = (await request.clone().json()) as { dry_run: boolean; rows: unknown[] }
      sent.push({ ...body, key: request.headers.get('Idempotency-Key') })
    }),
  )
  return sent
}

function open(onImported = vi.fn()) {
  return { onImported, ...renderPage(<SpoolImport open onOpenChange={() => {}} onImported={onImported} />) }
}

const csv = (text: string) => new File([text], 'spools.csv', { type: 'text/csv' })

describe('the spools CSV import', () => {
  it('lists an error line and keeps Import off', async () => {
    const { user } = open()
    await user.upload(screen.getByLabelText('CSV file'), csv('number,note\r\n3,Top shelf\r\n999,x\r\n'))
    const table = await screen.findByRole('table', { name: 'Import preview' })
    const error = within(table).getByRole('row', { name: /line 3/i })
    expect(error).toHaveTextContent('no spool #999')
    expect(within(table).getByRole('row', { name: /line 2/i })).toHaveTextContent('Top shelf')
    expect(screen.getByRole('button', { name: /^Import/ })).toBeDisabled()
  })

  it('says which derived columns it ignores', async () => {
    const { user } = open()
    await user.upload(screen.getByLabelText('CSV file'), csv('number,grams_left,last_used_at\r\n3,10,\r\n'))
    expect(await screen.findByText(/Ignored: grams_left, last_used_at/)).toBeInTheDocument()
  })

  it('imports a clean preview with dry_run false and one key', async () => {
    const sent = watchImports()
    const { user, onImported } = open()
    await user.upload(screen.getByLabelText('CSV file'), csv('material,brand,colour_name\r\nPLA,Sunlu,Red\r\nPETG,Sunlu,Blue\r\n'))
    const button = await screen.findByRole('button', { name: 'Import 2 spools' })
    expect(button).toBeEnabled()
    await user.click(button)
    await waitFor(() => expect(onImported).toHaveBeenCalled())
    expect(sent.map((one) => one.dry_run)).toEqual([true, false])
    expect(sent[0]!.key).toBeNull()
    expect(sent[1]!.key).toMatch(/^[0-9a-f-]{36}$/)
    expect(sent[1]!.rows).toEqual([
      { material: 'PLA', brand: 'Sunlu', colour_name: 'Red' },
      { material: 'PETG', brand: 'Sunlu', colour_name: 'Blue' },
    ])
  })

  it('clears the file input, so the same file can be chosen again', async () => {
    const sent = watchImports()
    const { user } = open()
    const input = screen.getByLabelText<HTMLInputElement>('CSV file')
    const file = csv('material\r\nPLA\r\n')
    await user.upload(input, file)
    await screen.findByRole('table', { name: 'Import preview' })
    expect(input.value).toBe('')
    await user.upload(input, file)
    await waitFor(() => expect(sent.map((one) => one.dry_run)).toEqual([true, true]))
  })

  it('announces that the file is being checked while the dry run runs', async () => {
    server.use(
      http.post(IMPORT, async () => {
        await delay(150)
      }),
    )
    const { user } = open()
    await user.upload(screen.getByLabelText('CSV file'), csv('material\r\nPLA\r\n'))
    const status = screen.getByRole('status')
    expect(await within(status).findByText('Checking each line of the file')).toBeInTheDocument()
    expect(status).toHaveAttribute('aria-live', 'polite')
    await screen.findByRole('table', { name: 'Import preview' })
    expect(within(status).queryByText('Checking each line of the file')).toBeNull()
  })

  it('drops a dry run that answers after Cancel', async () => {
    server.use(
      http.post(IMPORT, async () => {
        await delay(150)
      }),
    )
    const { user } = open()
    await user.upload(screen.getByLabelText('CSV file'), csv('material\r\nPLA\r\n'))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await new Promise((resolve) => setTimeout(resolve, 400))
    expect(screen.queryByRole('table', { name: 'Import preview' })).toBeNull()
    expect(screen.queryByText('spools.csv')).toBeNull()
  })
})
