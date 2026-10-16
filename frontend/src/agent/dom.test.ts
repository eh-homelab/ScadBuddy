import { afterEach, describe, expect, it } from 'vitest'
import { activeDialog, findByRole } from './dom'

afterEach(() => {
  document.body.innerHTML = ''
})

describe('activeDialog', () => {
  it('scopes to a ui/Dialog beside the assistant, though it is not aria-modal there (#798)', () => {
    document.body.innerHTML = `
      <button>Generate</button>
      <div role="dialog" aria-label="Print" data-modal=""><button>Print it</button></div>`
    const dialog = document.querySelector('[role="dialog"]')
    expect(activeDialog()).toBe(dialog)
    // What is behind the dialog stays out of the agent's reach.
    expect(findByRole('button', 'Generate')).toEqual([])
    expect(findByRole('button', 'Print it')).toHaveLength(1)
  })

  it('does not treat a non-modal dialog as modal', () => {
    document.body.innerHTML = `<button>Generate</button><div role="dialog" aria-label="Note"></div>`
    expect(activeDialog()).toBeNull()
    expect(findByRole('button', 'Generate')).toHaveLength(1)
  })
})
