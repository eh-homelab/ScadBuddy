import { screen, within } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router'
import { describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { BUILTIN_SLUG, versionIds } from '../mocks/fixtures'
import { renderPage } from '../test/utils'
import { DuplicatedFrom, DuplicateModelButton } from './DuplicateModelButton'

function Where() {
  const { pathname } = useLocation()
  return <div data-testid="where">{pathname}</div>
}

function renderButton(slug: string, name: string, landOn?: 'source') {
  return renderPage(
    <Routes>
      <Route path="/" element={<DuplicateModelButton slug={slug} name={name} landOn={landOn} />} />
      <Route path="*" element={<Where />} />
    </Routes>,
  )
}

describe('DuplicateModelButton (#159)', () => {
  it('duplicates a built-in under the name given, then opens the copy', async () => {
    const { user } = renderButton(BUILTIN_SLUG, 'Keychain Template')

    await user.click(screen.getByRole('button', { name: 'Duplicate' }))
    const dialog = screen.getByRole('dialog', { name: 'Duplicate Keychain Template' })
    const field = within(dialog).getByRole('textbox', { name: 'Name' })
    expect(field).toHaveValue('Keychain Template copy')

    await user.clear(field)
    await user.type(field, 'My Keychain')
    await user.click(within(dialog).getByRole('button', { name: 'Duplicate' }))

    expect(await screen.findByTestId('where')).toHaveTextContent('/m/my-keychain')
    const copy = await api.getModel('my-keychain')
    expect(copy).toMatchObject({
      name: 'My Keychain',
      origin: 'mine',
      upstream: {
        id: BUILTIN_SLUG,
        path: '_builtin/keychain-template',
        base: versionIds.synced,
      },
    })
    // The built-in itself is untouched.
    expect((await api.getModel(BUILTIN_SLUG)).origin).toBe('builtin')
  })

  it('duplicates a model of mine with the prefilled name', async () => {
    const { user } = renderButton('name-keychain', 'Name Keychain')

    await user.click(screen.getByRole('button', { name: 'Duplicate' }))
    await user.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Duplicate' }),
    )

    expect(await screen.findByTestId('where')).toHaveTextContent('/m/name-keychain-copy')
    expect((await api.getModel('name-keychain-copy')).upstream?.id).toBe('name-keychain')
  })

  it('lands on the copy\'s source when asked to', async () => {
    const { user } = renderButton(BUILTIN_SLUG, 'Keychain Template', 'source')

    await user.click(screen.getByRole('button', { name: 'Duplicate' }))
    await user.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Duplicate' }),
    )

    expect(await screen.findByTestId('where')).toHaveTextContent(
      '/m/keychain-template-copy/source',
    )
  })

  it('shows a taken name in the dialog and stays put', async () => {
    const { user } = renderButton(BUILTIN_SLUG, 'Keychain Template')

    await user.click(screen.getByRole('button', { name: 'Duplicate' }))
    const dialog = screen.getByRole('dialog')
    const field = within(dialog).getByRole('textbox', { name: 'Name' })
    await user.clear(field)
    await user.type(field, 'Name Keychain')
    await user.click(within(dialog).getByRole('button', { name: 'Duplicate' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      "a model named 'name-keychain' already exists",
    )
    expect(screen.queryByTestId('where')).not.toBeInTheDocument()

    // A different name then goes through.
    await user.clear(field)
    await user.type(field, 'Another Keychain')
    await user.click(within(dialog).getByRole('button', { name: 'Duplicate' }))
    expect(await screen.findByTestId('where')).toHaveTextContent('/m/another-keychain')
  })

  it('shows a name that yields no slug in the dialog', async () => {
    const { user } = renderButton('name-keychain', 'Name Keychain')

    await user.click(screen.getByRole('button', { name: 'Duplicate' }))
    const dialog = screen.getByRole('dialog')
    const field = within(dialog).getByRole('textbox', { name: 'Name' })
    await user.clear(field)
    await user.type(field, '!!!')
    await user.click(within(dialog).getByRole('button', { name: 'Duplicate' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'does not yield a usable slug',
    )
  })

  it('will not send an empty name', async () => {
    const { user } = renderButton('name-keychain', 'Name Keychain')

    await user.click(screen.getByRole('button', { name: 'Duplicate' }))
    const dialog = screen.getByRole('dialog')
    await user.clear(within(dialog).getByRole('textbox', { name: 'Name' }))

    expect(within(dialog).getByRole('button', { name: 'Duplicate' })).toBeDisabled()
  })
})

describe('DuplicatedFrom (#159)', () => {
  it('links the upstream, by name when known', () => {
    renderPage(
      <DuplicatedFrom
        upstream={{ id: BUILTIN_SLUG, path: '_builtin/keychain-template', base: null }}
        name="Keychain Template"
      />,
    )
    expect(screen.getByRole('link', { name: 'Keychain Template' })).toHaveAttribute(
      'href',
      '/m/builtin%3Akeychain-template',
    )
  })

  it('says nothing for a template with no upstream', () => {
    renderPage(<DuplicatedFrom upstream={null} />)
    expect(screen.queryByTestId('duplicated-from')).not.toBeInTheDocument()
  })
})
