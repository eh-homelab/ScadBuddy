import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderPage } from '../../test/utils'
import { StillWorking } from './StillWorking'

describe('StillWorking (#6949 K1)', () => {
  it('shows still working with a link to the activity', () => {
    renderPage(<StillWorking id="op-1" href="/activity?q=op-1" />)
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Still working. This can take a moment; you can leave this page.')
    expect(screen.getByRole('link', { name: 'See it in Activities' })).toHaveAttribute('href', '/activity?q=op-1')
  })

  it('has no link when the operation has no page', () => {
    renderPage(<StillWorking id="op-2" />)
    expect(screen.getByRole('status')).toHaveTextContent('Still working.')
    expect(screen.queryByRole('link')).toBeNull()
  })
})
