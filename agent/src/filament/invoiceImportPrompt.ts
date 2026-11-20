// The first message of an invoice import session (#6267, plan 2026-11-18-filament-invoice-import.md
// Decision 4). The invoice itself is attached to the message, as an image or a PDF document block
// (sessions/images.ts); this text names the invoice in the database and the rules the session
// follows. The `filament` skill (plugins/scadbuddy/skills/filament/SKILL.md) holds the rest.

/** The prompt that starts the import of invoice `invoiceId`, whose file is attached to the message. */
export function invoiceImportPrompt(invoiceId: string): string {
  return [
    `Import the attached invoice into ScadBuddy's filament inventory. The invoice is draft ${invoiceId}. Read it with filament_invoice_get and edit that draft with filament_invoice_upsert (invoice_id ${invoiceId}). Do not make a second invoice.`,
    '',
    'First load the `filament` skill and follow it. These rules always apply:',
    '- Read the invoice from the attached file: its number, date, currency, subtotal, shipping, tax and total, and each line with its quantity and prices.',
    '- Diff before you create. For each line, call filament_invoice_candidates and update a spool that already matches before you make a new one. An archived (used-up) spool (`archived: true`) may be filled too: a spent spool was still bought. It takes the line cost and stays archived.',
    '- Offer uncosted candidates first, then costed ones. A costed candidate with no invoice_line_id is often a spool that the Bambuddy import costed: offer to link it to the line, show the person its old cost per kg and the new one, and add it to update_spool_ids only when the person says yes. The confirm replaces its cost. Never offer a spool that has an invoice_line_id: it belongs to another invoice.',
    '- The cost of one spool is (line subtotal + line tax + the line\'s share of shipping) ÷ quantity. Shipping is shared over the lines by value. The confirm computes this number; show the person the same one.',
    '- For a Bambu Lab line, find the colour with filament_bambu_colour, from the code in the SKU.',
    '- If a line is ambiguous (more than one possible spool, an unknown colour or material, a total that does not add up), ask the person. Do not guess.',
    '- Never confirm the invoice without the person. Show what you will confirm, and wait for a yes.',
  ].join('\n')
}
