import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Job } from '../api/types'
import { BUILTIN_SLUG } from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { CustomizePage } from '../pages/CustomizePage'
import { renderPage } from '../test/utils'

// WebGL does not exist in jsdom; the page is under test here, not the viewer.
vi.mock('./Preview', () => ({
  Preview: ({ job }: { job?: Job }) => (
    <div data-testid="preview">{job?.bbox_mm && <span data-testid="bbox">rendered</span>}</div>
  ),
}))

function render(slug = 'name-keychain') {
  return renderPage(<CustomizePage />, { route: `/m/${slug}`, path: '/m/:slug' })
}

async function picker() {
  const select = await screen.findByRole('combobox', { name: 'Preset' })
  await waitFor(() => expect(within(select).getByRole('option', { name: 'Tiny' })).toBeInTheDocument())
  return select
}

/** Every render request's params, so what was ASKED of the server can be asserted. */
function watchRenders(): Promise<{ params: Record<string, unknown> }>[] {
  const bodies: Promise<{ params: Record<string, unknown> }>[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/render')) {
      bodies.push(request.clone().json() as Promise<{ params: Record<string, unknown> }>)
    }
  })
  return bodies
}

describe('PresetPicker', () => {
  // Saves and deletes write the mock's state, which no global hook resets.
  beforeEach(() => resetMockState())

  it('lists the template\'s presets apart from the saved ones', async () => {
    render()
    const select = await picker()
    const shipped = within(select).getByRole('group', { name: 'From the template' })
    const saved = within(select).getByRole('group', { name: 'Saved' })
    expect(within(shipped).getAllByRole('option').map((o) => o.textContent)).toEqual(['Tiny'])
    expect(within(saved).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'Mum',
      'Old engraving',
    ])
  })

  it('applies a preset over the defaults and renders it', async () => {
    const renders = watchRenders()
    const { user } = render()
    const select = await picker()
    // Another preset first, whose values the next one drops: it starts from the defaults.
    await user.selectOptions(select, 'Tiny')
    expect(screen.getByText('2 changed from defaults')).toBeInTheDocument()

    await user.selectOptions(select, 'Mum')
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Mum')
    expect(screen.getByText('3 changed from defaults')).toBeInTheDocument()

    await waitFor(async () => {
      const last = await renders.at(-1)
      expect(last?.params).toMatchObject({ name: 'Mum', body_color: '#222222' })
    }, { timeout: 4000 })
  })

  it('says which values it skipped because the template dropped them', async () => {
    const { user } = render()
    await user.selectOptions(await picker(), 'Old engraving')
    expect(screen.getByRole('status')).toHaveTextContent('engrave_depth')
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Ada')
  })

  it('saves what differs from the defaults as a new preset', async () => {
    const create = vi.spyOn(api, 'createPreset')
    const { user } = render()
    await picker()
    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.clear(name)
    await user.type(name, 'Nova')

    await user.click(screen.getByRole('button', { name: 'Save as preset…' }))
    const dialog = screen.getByRole('dialog', { name: 'Save as preset' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Preset name' }), 'Nova tag')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(create).toHaveBeenCalledWith('name-keychain', {
      name: 'Nova tag',
      params: { name: 'Nova' },
    })
    const select = screen.getByRole('combobox', { name: 'Preset' })
    expect(select).toHaveDisplayValue('Nova tag')
    expect(within(select).getByRole('group', { name: 'Saved' })).toHaveTextContent('Nova tag')
  })

  it('keeps the dialog open with the server\'s reason when the name is taken', async () => {
    const { user } = render()
    await picker()
    await user.click(screen.getByRole('button', { name: 'Save as preset…' }))
    const dialog = screen.getByRole('dialog', { name: 'Save as preset' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Preset name' }), 'mum')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('already has a preset')
  })

  it('updates a saved preset once its values are changed', async () => {
    const update = vi.spyOn(api, 'updatePreset')
    const { user } = render()
    await user.selectOptions(await picker(), 'Mum')
    expect(screen.queryByRole('button', { name: 'Update' })).not.toBeInTheDocument()

    await user.type(screen.getByRole('textbox', { name: 'Name on the tag' }), 'my')
    expect(screen.getByTestId('preset-modified')).toHaveTextContent('Changed from Mum')
    await user.click(screen.getByRole('button', { name: 'Update' }))

    await waitFor(() => expect(screen.queryByTestId('preset-modified')).not.toBeInTheDocument())
    expect(update).toHaveBeenCalledWith('name-keychain', 'a1b2c3d4e5f60718293a4b5c6d7e8f90', {
      params: { name: 'Mummy', body_color: '#222222', text_color: '#FFFFFF' },
    })
  })

  it('offers neither Update nor Delete on a preset the template ships', async () => {
    const { user } = render()
    await user.selectOptions(await picker(), 'Tiny')
    await user.type(screen.getByRole('textbox', { name: 'Name on the tag' }), 'x')
    expect(screen.getByTestId('preset-modified')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Update' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Delete preset/ })).not.toBeInTheDocument()
  })

  it('deletes a saved preset after confirming, leaving the values on screen', async () => {
    const { user } = render()
    const select = await picker()
    await user.selectOptions(select, 'Mum')
    await user.click(screen.getByRole('button', { name: 'Delete preset Mum' }))
    const dialog = screen.getByRole('dialog', { name: 'Delete preset Mum' })
    await user.click(within(dialog).getByRole('button', { name: 'Delete preset' }))

    await waitFor(() =>
      expect(within(select).queryByRole('option', { name: 'Mum' })).not.toBeInTheDocument(),
    )
    expect(select).toHaveValue('')
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Mum')
  })

  it('saves presets on a built-in template too', async () => {
    const { user } = render(BUILTIN_SLUG)
    await picker()
    await user.click(screen.getByRole('button', { name: 'Save as preset…' }))
    const dialog = screen.getByRole('dialog', { name: 'Save as preset' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Preset name' }), 'Mine')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Preset' })).toHaveDisplayValue('Mine'),
    )
  })
})
