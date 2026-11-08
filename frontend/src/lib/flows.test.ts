import { describe, expect, it } from 'vitest'
import { actorText, hopUntilText, markdownText } from './flows'

// #2482 — when a hop ends: the time alone today, with the date on any other day.
describe('hopUntilText', () => {
  it('says the time alone when the hop ends today', () => {
    const now = new Date(2026, 9, 13, 9, 0)
    const at = new Date(2026, 9, 13, 23, 30)
    expect(hopUntilText(at.toISOString(), now)).toBe(at.toLocaleTimeString())
  })

  it('names the date when the hop ends on another day', () => {
    const now = new Date(2026, 9, 13, 23, 55)
    const at = new Date(2026, 9, 14, 0, 5)
    expect(hopUntilText(at.toISOString(), now)).toBe(at.toLocaleString())
    expect(hopUntilText(at.toISOString(), now)).not.toBe(at.toLocaleTimeString())
  })
})

describe('markdownText (#3186)', () => {
  it('drops the markup and keeps the words, snake_case and arithmetic too', () => {
    expect(markdownText('_Nightly_ batch of `render_box`, __bold__ and 2 * 3 * 4')).toBe(
      'Nightly batch of render_box, bold and 2 * 3 * 4',
    )
    expect(markdownText('> quoted\n\n1. first\n2. ![alt](x.png) second\n\n```py\nrun()\n```')).toBe(
      'quoted first alt second run()',
    )
  })
})


// #4252 — the built-in flow's author is the build, never "someone else".
describe('actorText', () => {
  it('names a built-in author', () => {
    expect(actorText({ kind: 'builtin', name: 'order-fulfilment' })).toBe('ScadBuddy (built in)')
    expect(actorText({ kind: 'other' })).toBe('someone else')
  })
})
