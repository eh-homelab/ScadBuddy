import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderPage } from '../../test/utils'
import { ContextBlocks } from './ContextBlocks'

// #8062 — a task's context blocks (R8): one key each, drawn as text and never as markup.

const draw = (items: Record<string, unknown>[]) => renderPage(<ContextBlocks items={items} />)

describe('context blocks', () => {
  it('draws text as Markdown and never as raw HTML', () => {
    const { container } = draw([{ text: '**Check** the loop <b>now</b> <script>alert(1)</script>' }])
    expect(screen.getByText('Check').tagName).toBe('STRONG')
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('b')).toBeNull()
    expect(container).toHaveTextContent('<b>now</b>')
  })

  it('draws a table as a table', () => {
    draw([{ table: { columns: ['Part', 'Qty'], rows: [['Clip', 4], ['Key ring', 2]] } }])
    const table = screen.getByRole('table')
    expect(within(table).getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual(['Part', 'Qty'])
    expect(within(table).getAllByRole('row')).toHaveLength(3)
    expect(within(table).getByRole('cell', { name: 'Key ring' })).toBeInTheDocument()
  })

  it('draws a checklist whose ticks stay on the screen', async () => {
    const { user } = draw([{ checklist: ['Wipe the bed', 'Load the pink spool'] }])
    const tick = screen.getByRole('checkbox', { name: 'Wipe the bed' })
    expect(tick).not.toBeChecked()
    await user.click(tick)
    expect(tick).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Load the pink spool' })).not.toBeChecked()
  })

  it('draws what a weight should be', () => {
    draw([{ expect: { weight_g: 640, tolerance_g: 96 } }])
    expect(screen.getByText('Expect 640 g ± 96 g')).toBeInTheDocument()
  })

  it('draws an entity as a link to its page', () => {
    draw([{ entity: 'order:abc-1' }, { entity: 'spool:s-9' }])
    expect(screen.getByRole('link', { name: /order/i })).toHaveAttribute('href', '/orders/abc-1')
    expect(screen.getByRole('link', { name: /spool/i })).toHaveAttribute('href', '/filament/spools/s-9')
  })

  it('draws an image from the attachment content route', () => {
    draw([{ image: { entity: 'task:t-1', attachment_id: 'a-7' } }])
    expect(screen.getByRole('img')).toHaveAttribute('src', '/api/v1/entities/task%3At-1/attachments/a-7/content')
  })

  it('draws any other dict as a definition list', () => {
    const { container } = draw([{ order: 'SB-1042', note: 'Ring the bell', items: ['a', 'b'] }, { text: 'x', extra: 1 }])
    const terms = [...container.querySelectorAll('dt')].map((term) => term.textContent)
    expect(terms).toEqual(['order', 'note', 'items', 'text', 'extra'])
    expect(container.querySelector('dd')).toHaveTextContent('SB-1042')
  })

  it('draws a block of a wrong shape as a definition list', () => {
    const { container } = draw([{ table: 'nope' }, { checklist: [1] }])
    expect([...container.querySelectorAll('dt')].map((term) => term.textContent)).toEqual(['table', 'checklist'])
  })

  it('draws nothing for no context', () => {
    const { container } = draw([{}])
    expect(container).toBeEmptyDOMElement()
  })
})
