import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { Param } from '../../api/types'
import { spoolColour } from '../../lib/filamentColour'
import { server } from '../../mocks/server'
import { ColorWidget } from './ColorWidget'

const param: Param = { group: 'Colours', name: 'body_color', type: 'color', initial: '#1B6CA8', caption: 'Plate' }

function setup(value = '#1B6CA8') {
  const onChange = vi.fn()
  const user = userEvent.setup()
  render(<ColorWidget param={param} value={value} onChange={onChange} />)
  return { onChange, user }
}

describe('a colour from a filament (#1026)', () => {
  it("lists Bambuddy's spools, loaded first, and takes the chosen one's colour", async () => {
    let reads = 0
    server.use(
      http.get('/api/v1/farm/inventory', () => {
        reads += 1
        return undefined
      }),
    )
    const { onChange, user } = setup()
    // Not read until asked for.
    expect(reads).toBe(0)

    await user.click(screen.getByRole('button', { name: 'Choose from a filament' }))
    const list = await screen.findByRole('list', { name: 'Filaments for Plate' })
    const options = within(list).getAllByRole('button')
    expect(options.map((option) => option.textContent)).toEqual([
      'Insignia PETG — Clearclear, no tint recorded',
      'Bambu Lab PLA Basic — Mistletoe Green1000 g',
      'Bambu Lab PLA Basic — Hot Pink930 g',
    ])
    // A clear spool with no tint has no colour to give.
    expect(options[0]).toBeDisabled()

    await user.click(within(list).getByRole('button', { name: /Hot Pink/ }))
    expect(onChange).toHaveBeenCalledWith('#F5547C')
    expect(screen.queryByRole('list', { name: 'Filaments for Plate' })).toBeNull()
  })

  it('marks the spool whose colour the parameter already has', async () => {
    const { user } = setup('#3F8E43')
    await user.click(screen.getByRole('button', { name: 'Choose from a filament' }))
    expect(await screen.findByRole('button', { name: /Mistletoe Green/ })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: /Hot Pink/ })).toHaveAttribute('aria-pressed', 'false')
  })

  it('says why when Bambuddy cannot be read', async () => {
    server.use(
      http.get('/api/v1/farm/inventory', () =>
        HttpResponse.json({ detail: 'Bambuddy is not configured' }, { status: 409 }),
      ),
    )
    const { user } = setup()
    await user.click(screen.getByRole('button', { name: 'Choose from a filament' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Bambuddy is not configured')
  })
})

describe('spoolColour', () => {
  it.each([
    ['F5547CFF', { hex: '#F5547C', translucent: false }],
    ['77EDD780', { hex: '#77EDD7', translucent: true }],
    ['#0086D6', { hex: '#0086D6', translucent: false }],
    ['00000000', null],
    ['nonsense', null],
    [null, null],
  ])('%s', (rgba, colour) => {
    expect(spoolColour({ rgba })).toEqual(colour)
  })
})
