import { fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { sameOverrides, sentOverrides, settingText } from '../../lib/processOverrides'
import { SHOWN_ROWS, SlicerSettingsEditor } from './SlicerSettingsEditor'

// #2998 — the generic "Slicer settings" list: every key of the process, searchable, with
// any other key added by name; only a change is kept, and nothing is refused.

const BASELINE = { skirt_loops: '0', enable_prime_tower: '1', outer_wall_speed: ['200', '200'], brim_type: 'auto_brim' }

function Harness({ onValue, baseline = BASELINE }: { onValue: (v: Record<string, unknown>) => void; baseline?: Record<string, unknown> | null }) {
  const [value, setValue] = useState<Record<string, unknown>>({})
  return (
    <SlicerSettingsEditor
      id="slicer-settings"
      title="Slicer settings"
      value={value}
      baseline={baseline}
      onChange={(next) => {
        setValue(next)
        onValue(next)
      }}
    />
  )
}

function open() {
  fireEvent.click(screen.getByTestId('slicer-settings-toggle'))
  return screen.getByTestId('slicer-settings')
}

describe('SlicerSettingsEditor (#2998)', () => {
  it('lists every key with its process value and narrows them by search', () => {
    render(<Harness onValue={vi.fn()} />)
    const list = open()
    expect(within(list).getAllByRole('listitem')).toHaveLength(4)
    // A per-extruder value the same on both sides reads as one value.
    expect(within(list).getByTestId('slicer-settings-outer_wall_speed-value')).toHaveAttribute('placeholder', '200')
    fireEvent.change(within(list).getByTestId('slicer-settings-search'), { target: { value: 'SKIRT' } })
    expect(within(list).getAllByRole('listitem')).toHaveLength(1)
    expect(within(list).getByText('skirt_loops')).toBeInTheDocument()
  })

  it('keeps only a change, counts it, and shows it under Changed only', () => {
    const onValue = vi.fn()
    render(<Harness onValue={onValue} />)
    const list = open()
    fireEvent.change(within(list).getByTestId('slicer-settings-skirt_loops-value'), { target: { value: '2' } })
    expect(onValue).toHaveBeenLastCalledWith({ skirt_loops: '2' })
    // The process's own value is no change.
    fireEvent.change(within(list).getByTestId('slicer-settings-enable_prime_tower-value'), { target: { value: '1' } })
    expect(onValue).toHaveBeenLastCalledWith({ skirt_loops: '2' })
    expect(screen.getByTestId('slicer-settings-toggle')).toHaveTextContent('(1 changed)')
    fireEvent.click(within(list).getByTestId('slicer-settings-changed-only'))
    expect(within(list).getAllByRole('listitem')).toHaveLength(1)
    fireEvent.click(within(list).getByTestId('slicer-settings-skirt_loops-reset'))
    expect(onValue).toHaveBeenLastCalledWith({})
  })

  it('adds any key by name, with a note when the process has no value for it', () => {
    const onValue = vi.fn()
    render(<Harness onValue={onValue} />)
    const list = open()
    fireEvent.change(within(list).getByTestId('slicer-settings-new-key'), { target: { value: 'my_own_key' } })
    fireEvent.change(within(list).getByTestId('slicer-settings-new-value'), { target: { value: 'anything' } })
    fireEvent.click(within(list).getByTestId('slicer-settings-add'))
    expect(onValue).toHaveBeenLastCalledWith({ my_own_key: 'anything' })
    expect(within(list).getByTestId('slicer-settings-my_own_key-unknown')).toBeInTheDocument()
  })

  it('draws at most SHOWN_ROWS rows, and says so', () => {
    const many = Object.fromEntries(Array.from({ length: SHOWN_ROWS + 5 }, (_, n) => [`key_${n}`, '1']))
    render(<Harness onValue={vi.fn()} baseline={many} />)
    const list = open()
    expect(within(list).getAllByRole('listitem')).toHaveLength(SHOWN_ROWS)
    expect(within(list).getByText(`Showing ${SHOWN_ROWS} of ${SHOWN_ROWS + 5}. Search to narrow them.`)).toBeInTheDocument()
  })
})

describe('processOverrides (#2998)', () => {
  it('writes values as a Bambu config does and sends none for nothing set', () => {
    expect(settingText(true)).toBe('1')
    expect(settingText(['0.2', '0.3'])).toBe('0.2,0.3')
    expect(sentOverrides({ b: '1', a: null, c: 2 })).toEqual({ b: '1', c: 2 })
    expect(sentOverrides({})).toBeNull()
    expect(sameOverrides({ a: 1 }, { a: '1' })).toBe(true)
    expect(sameOverrides({ a: '1' }, null)).toBe(false)
  })
})
