import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { BUILTIN_SLUG } from '../../mocks/fixtures'
import { renderPage } from '../../test/utils'
import { MediaButton } from './MediaButton'

describe('MediaButton (#279)', () => {
  it('closes only the Duplicate dialog opened over it on Escape', async () => {
    const model = await api.getModel(BUILTIN_SLUG)
    const { user } = renderPage(<MediaButton model={model} />)

    await user.click(screen.getByRole('button', { name: 'Media' }))
    await user.click(screen.getByRole('button', { name: 'Duplicate' }))
    expect(screen.getByRole('dialog', { name: 'Duplicate Keychain Template' })).toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: 'Duplicate Keychain Template' })).not.toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Media' })).toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: 'Media' })).not.toBeInTheDocument()
  })
})
