import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ModalCompanionContext } from '../../lib/modal'
import { Confirm } from './Confirm'

function confirm(companion: HTMLElement | null) {
  return render(
    <ModalCompanionContext.Provider value={companion}>
      <Confirm
        trigger={<button type="button">Forget</button>}
        open
        onOpenChange={vi.fn()}
        title="Forget this run?"
        action="Forget run"
        onAction={vi.fn()}
        busy={false}
      >
        <p>This can't be undone.</p>
      </Confirm>
    </ModalCompanionContext.Provider>,
  )
}

describe('Confirm (#3653)', () => {
  it('is an alert dialog, modal with no assistant panel beside it', () => {
    confirm(null)
    const dialog = screen.getByRole('alertdialog', { name: 'Forget this run?' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog).toHaveAttribute('data-modal')
    expect(screen.getByRole('button', { name: 'Forget run' })).toHaveAttribute('data-agent-user-only')
  })

  it('leaves the assistant panel usable beside it (#798)', () => {
    const panel = document.createElement('aside')
    document.body.append(panel)
    confirm(panel)
    expect(screen.getByRole('alertdialog', { name: 'Forget this run?' })).not.toHaveAttribute('aria-modal')
    expect(document.body.style.pointerEvents).not.toBe('none')
    panel.remove()
  })
})
