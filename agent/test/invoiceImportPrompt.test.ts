import { describe, expect, it } from 'vitest'
import { invoiceImportPrompt } from '../src/filament/invoiceImportPrompt.js'

// #6700 R-6700-4: a costed candidate with no invoice line is offered, with its old and new
// cost per kg, and linked only on a yes; uncosted candidates come first.
describe('invoiceImportPrompt', () => {
  const prompt = invoiceImportPrompt('00000000-0000-4000-8000-000000000001')

  it('offers a costed candidate with no invoice line, old and new cost per kg, linked only on a yes', () => {
    expect(prompt).toMatch(/costed candidate/i)
    expect(prompt).toContain('invoice_line_id')
    expect(prompt).toMatch(/old cost per kg and the new one/i)
    expect(prompt).toMatch(/only when the person says yes/i)
  })

  it('places uncosted candidates before costed ones', () => {
    expect(prompt).toMatch(/uncosted candidates first, then costed ones/i)
  })

  it('never links a spool that already has an invoice line', () => {
    expect(prompt).toMatch(/never offer a spool that has an invoice_line_id/i)
  })
})
