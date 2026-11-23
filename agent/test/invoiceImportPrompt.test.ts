import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
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

  // #6707 R-6707-12: a used-up spool was still bought.
  it('lets a line fill an archived (used-up) spool, which stays archived', () => {
    expect(prompt).toMatch(/archived \(used-up\) spool/i)
    expect(prompt).toMatch(/stays archived/i)
  })

  it('never links a spool that already has an invoice line', () => {
    expect(prompt).toMatch(/never offer a spool that has an invoice_line_id/i)
  })

  // #6912 R-6912-6: a type's colour comes from the catalog before the person is asked, after
  // Bambu's code table for a Bambu Lab SKU.
  it('looks a new type\'s colour up in the catalog after filament_bambu_colour, before it asks the person', () => {
    const bambu = prompt.indexOf('filament_bambu_colour')
    const catalog = prompt.indexOf('filament_catalog_list')
    const ask = prompt.search(/ask the person\. Do not guess/)
    expect(bambu).toBeGreaterThan(-1)
    expect(catalog).toBeGreaterThan(bambu)
    expect(ask).toBeGreaterThan(catalog)
    expect(prompt).toMatch(/filament_catalog_list with kind colours and q/)
  })

  // #7025: a 3DFP id comes from the worker's search, never a web search; a create only on a yes.
  it('finds a 3DFP id with filament_tdfp_search and creates only after a yes', () => {
    expect(prompt).toContain('filament_tdfp_search')
    expect(prompt).toMatch(/never with a web search/)
    expect(prompt).toMatch(/filament_tdfp_create, and call it only after the person says yes/)
    const skill = readFileSync(fileURLToPath(new URL('../plugins/scadbuddy/skills/filament/SKILL.md', import.meta.url)), 'utf8')
    expect(skill).toContain('`filament_tdfp_search`')
    expect(skill).toMatch(/never with a\s+web search/)
  })

  // The skill says the same, in the same order (read through the agent's copy of the plugin).
  it('is the filament skill\'s order too', () => {
    const skill = readFileSync(fileURLToPath(new URL('../plugins/scadbuddy/skills/filament/SKILL.md', import.meta.url)), 'utf8')
    const tools = skill.slice(skill.indexOf('Tools, in the order you use them'))
    expect(tools.indexOf('`filament_catalog_list`')).toBeGreaterThan(tools.indexOf('`filament_bambu_colour`'))
    expect(tools.indexOf('`filament_catalog_list`')).toBeLessThan(tools.indexOf('`filament_invoice_candidates`'))
    expect(skill).toMatch(/kind `colours`/)
  })
})
