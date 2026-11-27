import { fireEvent, screen, waitFor } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { SchemaForm } from './SchemaForm'

// #8062 — the widgets a task's form draws from `x-sb-widget` and the plain types.

function draw(
  properties: Record<string, unknown>,
  { required = [], taskId = 'task-1', expect: target }: { required?: string[]; taskId?: string; expect?: { weight_g: number; tolerance_g?: number } } = {},
) {
  const onSubmit = vi.fn()
  const schema = { type: 'object', properties, required }
  const view = renderPage(
    <SchemaForm answer schema={schema} taskId={taskId} expect={target} submitLabel="Answer" busy={false} onSubmit={onSubmit} />,
  )
  return { ...view, onSubmit }
}

const press = () => screen.getByRole('button', { name: 'Answer' })

describe('task widgets (#8062)', () => {
  it('draws an enum of three options as a radiogroup of buttons and six as a select', async () => {
    const { user, onSubmit } = draw({
      size: { type: 'string', enum: ['S', 'M', 'L'], title: 'Size' },
      colour: { type: 'string', enum: ['a', 'b', 'c', 'd', 'e', 'f'], title: 'Colour' },
    })
    const group = screen.getByRole('radiogroup', { name: /Size/ })
    expect(group).toBeInTheDocument()
    await user.click(screen.getByRole('radio', { name: 'M' }))
    expect(screen.getByRole('radio', { name: 'M' })).toBeChecked()
    await choose(user, screen.getByRole('combobox', { name: /Colour/ }), 'd')
    await user.click(press())
    expect(onSubmit).toHaveBeenCalledWith({ size: 'M', colour: 'd' })
  })

  it('draws a boolean as a switch', async () => {
    const { user, onSubmit } = draw({ ok: { type: 'boolean', title: 'Looks fine' } })
    const control = screen.getByRole('switch', { name: /Looks fine/ })
    await user.click(control)
    expect(control).toBeChecked()
    await user.click(press())
    expect(onSubmit).toHaveBeenCalledWith({ ok: true })
  })

  it('draws a bounded integer as a stepper that stops at its bounds', async () => {
    const { user, onSubmit } = draw({ n: { type: 'integer', minimum: 0, maximum: 2, default: 1, title: 'Count' } })
    const less = screen.getByRole('button', { name: 'Less' })
    const more = screen.getByRole('button', { name: 'More' })
    await user.click(more)
    expect(more).toBeDisabled()
    expect(screen.getByLabelText(/Count/)).toHaveValue(2)
    await user.click(less)
    await user.click(less)
    expect(less).toBeDisabled()
    expect(screen.getByLabelText(/Count/)).toHaveValue(0)
    await user.click(more)
    await user.click(press())
    expect(onSubmit).toHaveBeenCalledWith({ n: 1 })
  })

  it('takes a typed code and shows the place it names', async () => {
    const { user, onSubmit } = draw({ place: { 'x-sb-widget': 'scan', type: 'string', title: 'Shelf', expects: { kinds: ['location'] } } })
    server.use(
      http.get('/api/v1/codes/:code', () =>
        HttpResponse.json({ kind: 'location', id: 'l7', code: 'L-7', name: 'Shelf seven', archived: false }),
      ),
    )
    await user.type(screen.getByLabelText(/Shelf/), 'L-7{Enter}')
    expect(await screen.findByText('Shelf seven')).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
    await user.click(press())
    expect(onSubmit).toHaveBeenCalledWith({ place: 'L-7' })
  })

  it('looks a typed code up when the field is left, with no Enter', async () => {
    const { user, onSubmit } = draw({ place: { 'x-sb-widget': 'scan', type: 'string', title: 'Shelf' } })
    server.use(
      http.get('/api/v1/codes/:code', () =>
        HttpResponse.json({ kind: 'location', id: 'l7', code: 'L-7', name: 'Shelf seven', archived: false }),
      ),
    )
    await user.type(screen.getByLabelText(/Shelf/), 'L-7')
    await user.tab()
    expect(await screen.findByText('Shelf seven')).toBeInTheDocument()
    await user.click(press())
    expect(onSubmit).toHaveBeenCalledWith({ place: 'L-7' })
  })

  it('says what a code of the wrong kind should have been', async () => {
    const { user, onSubmit } = draw({ spool: { 'x-sb-widget': 'scan', type: 'string', title: 'Spool', expects: { kinds: ['spool'] } } }, { required: ['spool'] })
    server.use(
      http.get('/api/v1/codes/:code', () =>
        HttpResponse.json({ kind: 'location', id: 'l7', code: 'L-7', name: 'Shelf seven', archived: false }),
      ),
    )
    await user.type(screen.getByLabelText(/Spool/), 'L-7{Enter}')
    expect(await screen.findByText('Expected a spool')).toBeInTheDocument()
    expect(screen.getByLabelText(/Spool/)).toHaveAttribute('aria-invalid', 'true')
    // The rejected code is no answer.
    await user.click(press())
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('drops a lookup that the person has typed past', async () => {
    const { user } = draw({ spool: { 'x-sb-widget': 'scan', type: 'string', title: 'Spool' } })
    server.use(
      http.get('/api/v1/codes/:code', async ({ params }) => {
        if (params['code'] === 'S-1') await delay(150)
        return HttpResponse.json({ kind: 'spool', id: 'x', code: String(params['code']), name: `Name of ${String(params['code'])}`, archived: false })
      }),
    )
    const box = screen.getByLabelText(/Spool/)
    await user.type(box, 'S-1{Enter}')
    await user.clear(box)
    await user.type(box, 'S-2{Enter}')
    expect(await screen.findByText('Name of S-2')).toBeInTheDocument()
    await delay(300)
    expect(screen.queryByText('Name of S-1')).not.toBeInTheDocument()
    expect(box).toHaveValue('S-2')
  })

  it('moves and chooses with the arrow keys, Home and End', async () => {
    const { user } = draw({ size: { type: 'string', enum: ['S', 'M', 'L'], title: 'Size' } })
    const radio = (name: string) => screen.getByRole('radio', { name })
    expect(radio('S')).toHaveAttribute('tabindex', '0')
    expect(radio('M')).toHaveAttribute('tabindex', '-1')
    await user.tab()
    expect(radio('S')).toHaveFocus()
    await user.keyboard('{ArrowRight}')
    expect(radio('M')).toBeChecked()
    expect(radio('M')).toHaveFocus()
    expect(radio('M')).toHaveAttribute('tabindex', '0')
    await user.keyboard('{End}')
    expect(radio('L')).toBeChecked()
    await user.keyboard('{ArrowDown}')
    expect(radio('S')).toBeChecked()
    await user.keyboard('{ArrowLeft}')
    expect(radio('L')).toBeChecked()
    await user.keyboard('{Home}')
    expect(radio('S')).toBeChecked()
  })

  it('shows a failed upload and leaves the answer empty', async () => {
    const { user, onSubmit } = draw({ shot: { 'x-sb-widget': 'photo', type: 'string', title: 'Photo' } }, { required: ['shot'] })
    server.use(http.post('/api/v1/entities/:ref/attachments', () => HttpResponse.json({ title: 'Too large', status: 413, detail: 'the file is too large' }, { status: 413 })))
    await user.upload(screen.getByLabelText(/Photo/), new File(['x'], 'big.jpg', { type: 'image/jpeg' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/too large/i)
    await user.click(press())
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('says when no thing has the code', async () => {
    const { user } = draw({ spool: { 'x-sb-widget': 'scan', type: 'string', title: 'Spool' } })
    server.use(http.get('/api/v1/codes/:code', () => HttpResponse.json({ title: 'Not found', status: 404, detail: 'none' }, { status: 404 })))
    await user.type(screen.getByLabelText(/Spool/), 'Z-9{Enter}')
    expect(await screen.findByText('No thing has this code')).toBeInTheDocument()
  })

  it('uploads a photo and answers with the attachment id', async () => {
    const { user, onSubmit } = draw({ shot: { 'x-sb-widget': 'photo', type: 'string', title: 'Photo of the part' } }, { taskId: 't9' })
    const sent: string[] = []
    server.use(
      http.post('/api/v1/entities/:ref/attachments', ({ params }) => {
        sent.push(decodeURIComponent(String(params['ref'])))
        return HttpResponse.json({ id: 'att-42', filename: 'part.jpg' })
      }),
    )
    const input = screen.getByLabelText(/Photo of the part/)
    await user.upload(input, new File(['jpg'], 'part.jpg', { type: 'image/jpeg' }))
    expect(await screen.findByText(/part\.jpg/)).toBeInTheDocument()
    await user.click(press())
    expect(sent).toEqual(['task:t9'])
    expect(onSubmit).toHaveBeenCalledWith({ shot: 'att-42' })
  })

  it('draws a file field that takes any file', () => {
    draw({ doc: { 'x-sb-widget': 'file', type: 'string', title: 'Delivery note' } })
    expect(screen.getByLabelText(/Delivery note/)).not.toHaveAttribute('accept')
  })

  it('opens the library picker for a library_file entity and answers with the file id', async () => {
    const { user, onSubmit } = draw({ file: { 'x-sb-widget': 'entity', kind: 'library_file', type: 'string', title: 'Model' } })
    await user.type(screen.getByRole('searchbox'), 'a')
    const hit = await screen.findAllByRole('button', { name: /\.(3mf|stl)/i })
    await user.click(hit[0]!)
    await user.click(press())
    expect(onSubmit).toHaveBeenCalledWith({ file: expect.stringMatching(/^\d+$/) as unknown })
  })

  it('draws a location entity as a scan field', () => {
    draw({ place: { 'x-sb-widget': 'entity', kind: 'location', type: 'string', title: 'Place' } })
    expect(screen.getByLabelText(/Place/)).toHaveAttribute('data-widget', 'scan')
  })

  it('takes weight in grams and shows the target', async () => {
    const { user, onSubmit } = draw(
      { grams: { 'x-sb-widget': 'weight', type: 'number', title: 'Weight' } },
      { expect: { weight_g: 640, tolerance_g: 96 } },
    )
    expect(screen.getByText('Expect 640 g ± 96 g')).toBeInTheDocument()
    await user.type(screen.getByLabelText(/Weight/), '612.5')
    expect(screen.getByText('g')).toBeInTheDocument()
    await user.click(press())
    expect(onSubmit).toHaveBeenCalledWith({ grams: 612.5 })
  })

  it('draws confirm as one large button that submits true', async () => {
    const { user, onSubmit } = draw({ done: { 'x-sb-widget': 'confirm', type: 'boolean', title: 'Plate is clear' } })
    await user.click(screen.getByRole('button', { name: 'Plate is clear' }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith({ done: true }))
  })

  it('draws the plain field for an unknown widget and logs it', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    draw({ note: { 'x-sb-widget': 'hologram', type: 'string', title: 'Note' } })
    expect(screen.getByLabelText(/Note/)).toHaveAttribute('type', 'text')
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('hologram'))
    warn.mockRestore()
    fireEvent.click(press())
  })
})
