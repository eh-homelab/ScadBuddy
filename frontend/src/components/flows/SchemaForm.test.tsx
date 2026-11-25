import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { AnswerProblem } from '../../api/types'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { SchemaForm } from './SchemaForm'

// #7364 — the form drawn from a JSON schema: a flow's inputs, and a prompt's answer.

const EMAIL = { type: 'string', format: 'email', title: 'Email' }

function draw(
  properties: Record<string, unknown>,
  { required = [], problems, busy = false }: { required?: string[]; problems?: AnswerProblem[]; busy?: boolean } = {},
) {
  const onSubmit = vi.fn()
  const schema = { type: 'object', properties, required }
  const form = (next: AnswerProblem[] | undefined) => (
    <SchemaForm answer schema={schema} submitLabel="Answer" busyLabel="Sending…" busy={busy} problems={next} onSubmit={onSubmit} />
  )
  const view = renderPage(form(problems))
  return { ...view, onSubmit, again: (next: AnswerProblem[]) => view.rerender(form(next)) }
}

const press = (name = 'Answer') => screen.getByRole('button', { name })

describe('SchemaForm (#7364)', () => {
  it('draws an email field, with its problem under it and the root problem above the button', () => {
    draw(
      { email: EMAIL },
      { problems: [{ field: 'email', message: 'not a valid address' }, { field: '', message: 'email is required' }] },
    )
    const email = screen.getByLabelText(/^Email/)
    expect(email).toHaveAttribute('type', 'email')
    expect(email).toHaveAttribute('autocomplete', 'email')
    expect(email).toHaveAttribute('aria-invalid', 'true')
    expect(email).toHaveAccessibleDescription('not a valid address')
    const root = screen.getByRole('alert')
    expect(root).toHaveTextContent('email is required')
    expect(root.compareDocumentPosition(press()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(email.compareDocumentPosition(root) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('draws a date and a date-time, and no native check blocks an answer', async () => {
    const { user, onSubmit } = draw({
      email: EMAIL,
      day: { type: 'string', format: 'date' },
      at: { type: 'string', format: 'date-time' },
    })
    expect(screen.getByLabelText('day')).toHaveAttribute('type', 'date')
    expect(screen.getByLabelText('at')).toHaveAttribute('type', 'datetime-local')
    expect(press().closest('form')).toHaveAttribute('novalidate')
    await user.type(screen.getByLabelText(/^Email/), 'jo@host')
    fireEvent.change(screen.getByLabelText('day'), { target: { value: '2026-07-01' } })
    fireEvent.change(screen.getByLabelText('at'), { target: { value: '2026-07-01T09:30' } })
    await user.click(press())
    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(onSubmit.mock.calls[0]![0]).toEqual({
      email: 'jo@host',
      day: '2026-07-01',
      at: expect.stringMatching(/^2026-07-01T09:30:00[+-]\d\d:\d\d$/),
    })
  })

  it('leaves out an optional field left empty and always sends a boolean', async () => {
    const { user, onSubmit } = draw({
      email: EMAIL,
      colour: { type: 'string', enum: ['red', 'blue'] },
      copies: { type: 'integer', default: 2 },
      size: { type: 'number' },
      tags: { type: 'array', items: { type: 'string' } },
      also_sms: { type: 'boolean' },
    })
    await user.click(press())
    expect(onSubmit).toHaveBeenLastCalledWith({ copies: 2, also_sms: false })
    await choose(user, screen.getByRole('combobox', { name: /colour/ }), 'blue')
    await user.type(screen.getByRole('spinbutton', { name: /size/ }), '1.5')
    await user.type(screen.getByRole('textbox', { name: /tags/ }), 'a{Enter}b')
    await user.click(screen.getByRole('checkbox', { name: /also_sms/ }))
    await user.click(press())
    expect(onSubmit).toHaveBeenLastCalledWith({ colour: 'blue', copies: 2, size: 1.5, tags: ['a', 'b'], also_sms: true })
  })

  it('says a required field is required before anything is sent, and moves to it', async () => {
    const { user, onSubmit } = draw({ note: { type: 'string' }, email: EMAIL }, { required: ['email'] })
    await user.click(press())
    const email = screen.getByLabelText(/^Email/)
    expect(email).toHaveAccessibleDescription('Required')
    expect(email).toHaveFocus()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('says what an emptied field with a default becomes', async () => {
    const { user, onSubmit } = draw(
      { note: { type: 'string', default: 'hello' }, name: { type: 'string', default: 'Jo' }, other: { type: 'string', default: '' } },
      { required: ['name'] },
    )
    const note = screen.getByLabelText('note')
    expect(note).not.toHaveAccessibleDescription()
    expect(screen.getByLabelText('other')).not.toHaveAccessibleDescription()
    await user.clear(note)
    expect(note).toHaveAccessibleDescription('Left empty, this is hello.')
    await user.click(press())
    expect(onSubmit).toHaveBeenLastCalledWith({ name: 'Jo' })
    const name = screen.getByLabelText(/^name/)
    expect(name).toHaveAttribute('aria-required', 'true')
    await user.clear(name)
    await user.click(press())
    expect(name).toHaveAccessibleDescription('Required')
    expect(onSubmit).toHaveBeenCalledTimes(1)
  })

  it('does not send a value the browser could not read', async () => {
    const { user, onSubmit } = draw({ at: { type: 'string', format: 'date-time' } })
    const at = screen.getByLabelText<HTMLInputElement>('at')
    // A date with no time: the browser's value is empty, and it says the input is bad.
    Object.defineProperty(at, 'validity', { value: { badInput: true } })
    await user.click(press())
    expect(at).toHaveAccessibleDescription('This is not complete')
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('says a number it could not read must be a number', async () => {
    const { user, onSubmit } = draw({ size: { type: 'number' }, copies: { type: 'integer' } }, { required: ['copies'] })
    for (const name of ['size', 'copies']) {
      Object.defineProperty(screen.getByRole('spinbutton', { name: new RegExp(name) }), 'validity', { value: { badInput: true } })
    }
    await user.click(press())
    expect(screen.getByRole('spinbutton', { name: /size/ })).toHaveAccessibleDescription('A number')
    expect(screen.getByRole('spinbutton', { name: /copies/ })).toHaveAccessibleDescription('A whole number')
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('names the line the person typed, not the item sent, and keeps it through an edit', async () => {
    const { user, again, onSubmit } = draw({ tags: { type: 'array', items: { type: 'string', format: 'email' } } })
    const tags = screen.getByRole('textbox', { name: /tags/ })
    await user.type(tags, 'a{Enter}{Enter}b{Enter}c')
    await user.click(press())
    expect(onSubmit).toHaveBeenLastCalledWith({ tags: ['a', 'b', 'c'] })
    again([{ field: 'tags/1', message: 'not a valid email' }])
    expect(tags).toHaveAccessibleDescription('One per line. Line 3: not a valid email')
    await user.clear(tags)
    expect(tags).toHaveAccessibleDescription('One per line. Line 3: not a valid email')
  })

  it('says what an emptied date-time becomes in local time', async () => {
    const { user } = draw({ at: { type: 'string', format: 'date-time', default: '2026-12-01T17:30:00Z' } })
    const at = screen.getByLabelText('at')
    await user.clear(at)
    expect(at).toHaveAccessibleDescription(`Left empty, this is ${new Date('2026-12-01T17:30:00Z').toLocaleString()}.`)
  })

  it('wraps a long field title', () => {
    draw({ a: { type: 'string', title: 'x'.repeat(300) }, b: { type: 'boolean', title: 'y'.repeat(300) } })
    for (const label of [screen.getByText(/^x+/), screen.getByText(/^y+/)]) expect(label).toHaveClass('min-w-0', 'break-words')
  })

  it('wraps a long problem, of a field and of the whole form', () => {
    draw({ a: { type: 'string' } }, { problems: [{ field: 'a', message: 'x'.repeat(300) }, { field: 'y'.repeat(300), message: 'is odd' }] })
    for (const said of [screen.getByText(/^x+/), screen.getByText(/^y+/)]) expect(said).toHaveClass('min-w-0', 'break-words')
  })

  it('keeps what was typed when problems come back, and moves to the first bad field', async () => {
    const { user, again } = draw({
      note: { type: 'string' },
      email: EMAIL,
      tags: { type: 'array', items: { type: 'string', format: 'email' } },
    })
    await user.type(screen.getByLabelText(/^Email/), 'jo')
    await user.type(screen.getByRole('textbox', { name: /tags/ }), 'a')
    again([
      { field: 'tags/1', message: 'not a valid email' },
      { field: 'email', message: 'not a valid email' },
      { field: 'email', message: 'is shorter than 6 characters' },
      { field: 'gone', message: 'is odd' },
    ])
    const email = screen.getByLabelText(/^Email/)
    expect(email).toHaveValue('jo')
    expect(email).toHaveFocus()
    expect(email).toHaveAccessibleDescription('not a valid email; is shorter than 6 characters')
    expect(screen.getByRole('textbox', { name: /tags/ })).toHaveAccessibleDescription('One per line. Line 2: not a valid email')
    // A name the form does not draw has no place of its own.
    expect(screen.getByRole('alert')).toHaveTextContent('gone: is odd')
  })

  it('shows the answer in flight, and sends nothing more', async () => {
    const { user, onSubmit } = draw({ email: EMAIL }, { busy: true })
    const button = press('Sending…')
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('aria-busy', 'true')
    await user.type(screen.getByLabelText(/^Email/), 'jo@example.com{Enter}')
    fireEvent.submit(button.closest('form')!)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('says which field it cannot draw, and sends the rest', async () => {
    const { user, onSubmit } = draw({ where: { type: 'object', title: 'Where' }, note: { type: 'string' } })
    expect(screen.getByText('Where')).toBeInTheDocument()
    expect(screen.getByText('This field cannot be answered here.')).toBeInTheDocument()
    await user.type(screen.getByLabelText('note'), 'hi')
    await user.click(press())
    expect(onSubmit).toHaveBeenCalledWith({ note: 'hi' })
  })
})
