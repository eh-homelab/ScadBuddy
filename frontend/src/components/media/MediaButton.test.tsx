import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { BUILTIN_SLUG } from '../../mocks/fixtures'
import { renderPage } from '../../test/utils'
import { MediaButton } from './MediaButton'

describe('MediaButton (#279)', () => {
  it("opens a built-in's media to add to, and closes on Escape (#722)", async () => {
    const model = await api.getModel(BUILTIN_SLUG)
    const { user } = renderPage(<MediaButton model={model} />)

    await user.click(screen.getByRole('button', { name: 'Media' }))
    const dialog = screen.getByRole('dialog', { name: 'Media' })
    expect(dialog).toHaveTextContent('What the template ships, then the images and videos added to it.')
    expect(screen.getByLabelText('Add images or videos')).toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: 'Media' })).not.toBeInTheDocument()
  })
})
