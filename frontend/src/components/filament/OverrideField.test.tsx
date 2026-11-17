import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { OverrideField } from './OverrideField'

const base = { name: 'cost_per_kg', label: 'Cost per kg', value: '', effective: '15.91', overridden: false, hasType: true, onChange: () => {}, onReset: () => {} }

describe('OverrideField', () => {
  it('shows the type value as the placeholder and no mark while the spool sets nothing', () => {
    render(<OverrideField {...base} />)
    expect(screen.getByLabelText('Cost per kg')).toHaveAttribute('placeholder', '15.91')
    expect(screen.queryByText('Set on this spool')).toBeNull()
    expect(screen.queryByRole('button', { name: /to type/ })).toBeNull()
  })

  it('marks an overridden field and resets it', async () => {
    const user = userEvent.setup()
    const onReset = vi.fn()
    render(<OverrideField {...base} value="17.5" overridden onReset={onReset} />)
    expect(screen.getByText('Set on this spool')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Reset Cost per kg to type' }))
    expect(onReset).toHaveBeenCalledOnce()
  })

  it('has no mark and no reset on a spool without a type', () => {
    render(<OverrideField {...base} hasType={false} overridden value="17.5" />)
    expect(screen.queryByText('Set on this spool')).toBeNull()
    expect(screen.queryByRole('button', { name: /to type/ })).toBeNull()
  })

  it('reports typing', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<OverrideField {...base} onChange={onChange} />)
    await user.type(screen.getByLabelText('Cost per kg'), '2')
    expect(onChange).toHaveBeenCalledWith('2')
  })
})
