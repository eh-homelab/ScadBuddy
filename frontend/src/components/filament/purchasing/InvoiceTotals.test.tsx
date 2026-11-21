import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { InvoiceTotals } from './InvoiceTotals'

const show = (props: Partial<Parameters<typeof InvoiceTotals>[0]>) =>
  render(<InvoiceTotals currency="USD" exponent={2} subtotal="100.00" tax="7.00" taxBasis="receipt-wide 7.0%" shipping="10.00" lineSum="107.00" total="117.04" difference="0.04" reconciles {...props} />)

describe('the invoice totals', () => {
  it('shows a difference of 0.04 as reconciled', () => {
    show({})
    expect(screen.getByText('Difference').nextElementSibling).toHaveTextContent('0.04')
    expect(screen.getByRole('status')).toHaveTextContent(/reconciles/i)
    expect(screen.getByText(/receipt-wide 7\.0%/)).toBeInTheDocument()
  })

  it('warns with the amount when it does not reconcile', () => {
    show({ total: '117.50', difference: '0.50', reconciles: false })
    const mark = screen.getByRole('status')
    expect(mark).toHaveTextContent(/0\.50/)
    expect(mark).toHaveTextContent(/does not match/i)
    expect(mark).not.toHaveTextContent(/^reconciles/i)
  })

  it("shows every figure with the currency's own digits", () => {
    show({ currency: 'JPY', exponent: 0, subtotal: '1000', tax: '100', shipping: '0', lineSum: '1100', total: '1100', difference: '0', taxBasis: null })
    expect(screen.getByText('Total typed').nextElementSibling).toHaveTextContent('1,100')
    expect(screen.getByText('Total typed').nextElementSibling).not.toHaveTextContent('.')
  })
})
