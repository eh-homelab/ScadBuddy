// ScadBuddy template UI, host API v1 (docs/superpowers/specs/2026-09-27-template-pipelines-design.md §4):
// a house designer. It lists the pieces a box house needs and previews one at a time;
// Generate keeps the piece on screen. The whole house in one Generate is phase 4's pipeline.

import { DEFAULT_HOUSE, LIMITS, clampHouse, housePieces, pieceParams, pieceText, pieceTotal } from './pieces.js'

const DRIVEN = new Set(['piece', 'course', 'width_units', 'depth_units'])
const HOUSE_LABELS = {
  cols: 'Modules wide',
  rows: 'Modules deep',
  storeys: 'Storeys',
  windows: 'Windows per storey',
}

// The app's stylesheet reaches this shadow root, but only the utility classes the app
// itself uses exist in it, so the designer's layout and its own controls' look are its
// own (#736 may give templates a shared base sheet). Colours are the app's theme tokens,
// which cross the shadow boundary.
const CSS = `
.house { display: grid; grid-template-columns: minmax(260px, 340px) minmax(0, 1fr); height: 100%; min-height: 0; }
.side { min-height: 0; overflow-y: auto; border-right: 1px solid var(--sb-line); }
.form { display: flex; flex-direction: column; gap: 8px; padding: 12px; }
.form label { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 13px; }
.main { display: grid; grid-template-rows: minmax(0, 1fr) auto; min-height: 0; }
/* #1740 — the preview takes the larger share; the list scrolls within the rest. */
.bill { max-height: 30vh; overflow-y: auto; border-top: 1px solid var(--sb-line); }
.total { padding: 8px 12px; font-size: 12px; color: var(--sb-muted); }
.pieces { list-style: none; margin: 0; padding: 0; font-size: 13px; }
.pieces li { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 4px 12px; border-top: 1px solid var(--sb-line); }
/* #1739 — fields and buttons that look like them, 32 px tall (44 px for a finger). */
.form input, .pieces button {
  box-sizing: border-box; min-height: 32px; font: inherit; color: var(--sb-text);
  background: var(--sb-surface-2); border: 1px solid var(--sb-line-strong); border-radius: 4px;
}
.form input { width: 5.5em; padding: 0 8px; }
.pieces button { flex: none; min-width: 5.5em; padding: 0 12px; cursor: pointer; }
.form input:hover, .pieces button:hover { background: var(--sb-surface-3); border-color: var(--sb-faint); }
.pieces button[aria-pressed='true'] { background: var(--sb-accent); border-color: var(--sb-accent); color: var(--sb-accent-ink); font-weight: 600; }
.pieces li:has(button[aria-pressed='true']) { background: var(--sb-surface-2); }
@media (pointer: coarse) {
  .form input, .pieces button { min-height: 44px; }
}
/* #362, #1737 — a phone has no room for the form beside the preview: one column the box
   scrolls, the preview first at a height of its own, then the pieces, then the form. */
@media (max-width: 640px) {
  .house { grid-template-columns: minmax(0, 1fr); height: auto; }
  .main { display: contents; }
  sb-preview { display: block; order: 1; height: max(16rem, 40vh); }
  .bill { order: 2; max-height: none; overflow: visible; }
  .side { order: 3; overflow: visible; border-right: 0; border-top: 1px solid var(--sb-line); }
}
`

function element(tag, props = {}, ...children) {
  const el = document.createElement(tag)
  Object.assign(el, props)
  el.append(...children)
  return el
}

export async function mount(root, host) {
  const schema = await host.schema()
  // No `house` yet (a fresh template, an output saved before the designer) reads as the
  // default house; opening the designer never writes inputs nobody chose.

  const form = element('div', { className: 'form' })
  const numbers = {}
  for (const [key, [min, max]] of Object.entries(LIMITS)) {
    const input = element('input', { type: 'number', min: String(min), max: String(max), step: '1' })
    input.addEventListener('change', () => {
      const house = clampHouse({ ...host.inputs.get().house, [key]: input.value })
      host.inputs.set({ house })
    })
    numbers[key] = input
    form.append(element('label', {}, HOUSE_LABELS[key], input))
  }

  // Every other parameter is the host's own widget, the Grid sizes included (one value
  // for every piece of a house); the piece, its course and its size in modules are the
  // designer's to set.
  const style = element('div')
  for (const param of schema.parameters ?? []) {
    if (DRIVEN.has(param.name) || param.group === 'Piece') continue
    const field = element('sb-param')
    field.setAttribute('name', param.name)
    style.append(field)
  }

  const list = element('ul', { className: 'pieces' })
  const total = element('p', { className: 'total' })

  root.append(
    element('style', { textContent: CSS }),
    element(
      'div',
      { className: 'house' },
      element('div', { className: 'side' }, form, style),
      element(
        'div',
        { className: 'main' },
        element('sb-preview'),
        // No <sb-generate>: the page's own Generate sits below the designer (#1740).
        element('div', { className: 'bill' }, total, list),
      ),
    ),
  )

  // One row per entry, kept across draws: rebuilding the list would destroy the button
  // just pressed and drop keyboard focus to the page.
  const rows = new Map()
  function row(entry) {
    let found = rows.get(entry.id)
    if (!found) {
      const text = element('span')
      const show = element('button', { type: 'button' })
      show.dataset.piece = entry.piece
      show.dataset.entry = entry.id
      show.addEventListener('click', () => host.inputs.set({ params: pieceParams(entry) }))
      found = { li: element('li', {}, text, show), text, show }
      rows.set(entry.id, found)
    }
    return found
  }

  function draw(inputs) {
    const house = clampHouse(inputs.house ?? DEFAULT_HOUSE)
    for (const [key, input] of Object.entries(numbers)) input.value = String(house[key])
    const pieces = housePieces(house)
    total.textContent = `${pieceTotal(pieces)} pieces; Generate keeps the one shown.`
    const wanted = new Set(pieces.map((entry) => entry.id))
    for (const [id, { li }] of rows) {
      if (!wanted.has(id)) {
        li.remove()
        rows.delete(id)
      }
    }
    pieces.forEach((entry, index) => {
      const { li, text, show } = row(entry)
      const current =
        inputs.params?.piece === entry.piece &&
        (entry.course === null || inputs.params?.course === entry.course)
      text.textContent = pieceText(entry)
      // Pressed, not disabled: a disabled button would lose the focus it has.
      show.textContent = current ? 'Showing' : 'Show'
      // The name says which piece: a list of buttons that all read "Show" does not.
      show.setAttribute('aria-label', `${current ? 'Showing' : 'Show'} ${entry.label}`)
      show.setAttribute('aria-pressed', String(current))
      if (list.children[index] !== li) list.insertBefore(li, list.children[index] ?? null)
    })
  }

  draw(host.inputs.get())
  const unsubscribe = host.inputs.subscribe(draw)
  host.describe(() => {
    const inputs = host.inputs.get()
    const house = clampHouse(inputs.house ?? DEFAULT_HOUSE)
    const count = pieceTotal(housePieces(house))
    return `${house.cols}×${house.rows}-module house, ${house.storeys} storey(s), ${count} pieces; showing ${inputs.params?.piece ?? 'the default piece'}.`
  })
  return () => {
    unsubscribe()
    root.replaceChildren()
  }
}
