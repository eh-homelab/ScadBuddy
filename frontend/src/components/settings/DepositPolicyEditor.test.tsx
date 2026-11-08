import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { mockSettings, resetMockState, setMockSettings } from '../../mocks/handlers'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { DepositPolicyEditor } from './DepositPolicyEditor'

describe('DepositPolicyEditor (#5281)', () => {
  beforeEach(() => resetMockState())

  it('shows the saved rules and removes one', async () => {
    setMockSettings({
      ...mockSettings(),
      orders_deposit_policy: [
        { criterion: 'first_order', percent: '25' },
        { criterion: 'has_request', amount_minor: 2000 },
      ],
    })
    const { user } = renderPage(<DepositPolicyEditor />)
    const policy = screen.getByRole('region', { name: 'Deposit policy' })
    expect(await within(policy).findByLabelText('Rule 1 percent')).toHaveValue('25')
    expect(within(policy).getByLabelText('Rule 2 amount (USD)')).toHaveValue('20.00')
    expect(within(policy).getByText('A design request has no estimate, so a percent counts it as zero: use a fixed amount')).toBeInTheDocument()
    await user.click(within(policy).getByRole('button', { name: 'Remove rule 1' }))
    await user.click(within(policy).getByRole('button', { name: 'Save deposit policy' }))
    await waitFor(() => expect(mockSettings().orders_deposit_policy).toEqual([{ criterion: 'has_request', amount_minor: 2000 }]))
  })

  it('refuses a percent above 100 before it saves', async () => {
    const { user } = renderPage(<DepositPolicyEditor />)
    const policy = screen.getByRole('region', { name: 'Deposit policy' })
    await waitFor(() => expect(within(policy).getByRole('button', { name: 'Add rule' })).toBeEnabled())
    await user.click(within(policy).getByRole('button', { name: 'Add rule' }))
    await choose(user, within(policy).getByRole('combobox', { name: 'Rule 1 amount type' }), 'Percent')
    await user.type(within(policy).getByLabelText('Rule 1 percent'), '101')
    await user.click(within(policy).getByRole('button', { name: 'Save deposit policy' }))
    expect(await within(policy).findByRole('alert')).toHaveTextContent('Rule 1: enter a percent above 0 and at most 100, such as 50.')
    expect(mockSettings().orders_deposit_policy).toEqual([])
  })
})
