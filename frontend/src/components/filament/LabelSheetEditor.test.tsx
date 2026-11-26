import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { SavedLabelSheet } from '../../api/filament'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { LabelSheetEditor } from './LabelSheetEditor'

const SHEET: SavedLabelSheet = {
  id: '11111111-1111-4111-8111-111111111111',
  name: 'Shelf strip',
  layout: { page: 'letter', width_mm: 60, height_mm: 30, cols: 3, rows: 8, top_mm: 10, left_mm: 5, col_gap_mm: 2, row_gap_mm: 1 },
  created_at: '2026-11-25T10:00:00Z',
  updated_at: '2026-11-25T10:00:00Z',
}

/** Records every write to the sheet routes, and answers with `reply`. */
function record(reply: (body: Record<string, unknown>) => Response) {
  const writes: { method: string; path: string; body: Record<string, unknown> }[] = []
  const answer = async ({ request }: { request: Request }) => {
    const body = (await request.json()) as Record<string, unknown>
    writes.push({ method: request.method, path: new URL(request.url).pathname, body })
    return reply(body)
  }
  server.use(http.post('*/api/v1/filament/label-sheets', answer), http.put('*/api/v1/filament/label-sheets/:id', answer))
  return writes
}

const saved = (body: Record<string, unknown>, id = 'new-id') => HttpResponse.json({ ...SHEET, id, ...body }, { status: 201 })

async function fill(user: ReturnType<typeof renderPage>['user'], name: string, value: string) {
  const field = screen.getByLabelText(name)
  await user.clear(field)
  await user.type(field, value)
}

describe('LabelSheetEditor (#7216)', () => {
  it('saves a new sheet with numbers, not strings', async () => {
    const writes = record((body) => saved(body))
    const onSaved = vi.fn()
    const { user } = renderPage(<LabelSheetEditor sheet={null} onSaved={onSaved} onClose={() => {}} />)
    await fill(user, 'Name', 'Drawer fronts')
    await choose(user, screen.getByRole('combobox', { name: 'Page' }), 'Letter')
    await fill(user, 'Label width (mm)', '66.7')
    await fill(user, 'Label height (mm)', '25.4')
    await fill(user, 'Columns', '3')
    await fill(user, 'Rows', '10')
    await fill(user, 'Top margin (mm)', '12.7')
    await fill(user, 'Left margin (mm)', '4.8')
    await fill(user, 'Column gap (mm)', '3.2')
    await fill(user, 'Row gap (mm)', '0')
    await user.click(screen.getByRole('button', { name: 'Save sheet' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
    expect(writes).toEqual([
      {
        method: 'POST',
        path: '/api/v1/filament/label-sheets',
        body: {
          name: 'Drawer fronts',
          layout: { page: 'letter', width_mm: 66.7, height_mm: 25.4, cols: 3, rows: 10, top_mm: 12.7, left_mm: 4.8, col_gap_mm: 3.2, row_gap_mm: 0 },
        },
      },
    ])
    expect(onSaved.mock.calls[0]![0]).toMatchObject({ id: 'new-id', name: 'Drawer fronts' })
  })

  it('replaces the sheet it edits, by its id', async () => {
    const writes = record((body) => saved(body, SHEET.id))
    const onSaved = vi.fn()
    const { user } = renderPage(<LabelSheetEditor sheet={SHEET} onSaved={onSaved} onClose={() => {}} />)
    expect(screen.getByLabelText('Name')).toHaveValue('Shelf strip')
    expect(screen.getByLabelText('Rows')).toHaveValue(8)
    await fill(user, 'Rows', '7')
    await user.click(screen.getByRole('button', { name: 'Save sheet' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
    expect(writes).toEqual([{ method: 'PUT', path: `/api/v1/filament/label-sheets/${SHEET.id}`, body: { name: 'Shelf strip', layout: { ...SHEET.layout, rows: 7 } } }])
  })

  it('shows a taken name as the API says it, and keeps the form', async () => {
    record(() =>
      HttpResponse.json(
        { type: 'about:blank', title: 'Conflict', status: 409, detail: "a label sheet named 'Shelf strip' exists: choose another name", code: 'label_sheet_name_taken' },
        { status: 409 },
      ),
    )
    const onSaved = vi.fn()
    const { user } = renderPage(<LabelSheetEditor sheet={null} onSaved={onSaved} onClose={() => {}} />)
    await fill(user, 'Name', 'Shelf strip')
    await user.click(screen.getByRole('button', { name: 'Save sheet' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("a label sheet named 'Shelf strip' exists: choose another name")
    expect(screen.getByLabelText('Name')).toHaveValue('Shelf strip')
    expect(onSaved).not.toHaveBeenCalled()
  })

  it('shows the API’s fit message for a grid larger than its page', async () => {
    record(() =>
      HttpResponse.json(
        {
          type: 'about:blank',
          title: 'Unprocessable Content',
          status: 422,
          detail: 'the request did not match the expected shape',
          errors: [{ loc: ['body', 'layout'], msg: 'Value error, the grid is 232.5 mm wide, but the a4 page is 210 mm wide' }],
        },
        { status: 422 },
      ),
    )
    const onSaved = vi.fn()
    const { user } = renderPage(<LabelSheetEditor sheet={null} onSaved={onSaved} onClose={() => {}} />)
    await fill(user, 'Name', 'Too wide')
    await fill(user, 'Columns', '4')
    await user.click(screen.getByRole('button', { name: 'Save sheet' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('the grid is 232.5 mm wide, but the a4 page is 210 mm wide')
    expect(screen.getByLabelText('Columns')).toHaveValue(4)
    expect(onSaved).not.toHaveBeenCalled()
  })
})
