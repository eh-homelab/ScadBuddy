import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Job } from '../api/types'
import { BUILTIN_SLUG, keychainSchema } from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { CustomizePage } from '../pages/CustomizePage'
import { renderPage } from '../test/utils'
import { defaultValues } from '../lib/params'
import { PresetPicker } from './PresetPicker'

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
function watchRenders(): Promise<{ inputs: { params: Record<string, unknown> } }>[] {
  const bodies: Promise<{ inputs: { params: Record<string, unknown> } }>[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/render')) {
      bodies.push(request.clone().json() as Promise<{ inputs: { params: Record<string, unknown> } }>)
    }
  })
  return bodies
}

/** Opens Save as preset, names it and saves. */
async function savePresetNamed(user: ReturnType<typeof renderPage>['user'], name: string) {
  await user.click(await screen.findByRole('button', { name: 'Save as preset…' }))
  const dialog = screen.getByRole('dialog', { name: 'Save as preset' })
  await user.type(within(dialog).getByRole('textbox', { name: 'Preset name' }), name)
  await user.click(within(dialog).getByRole('button', { name: 'Save' }))
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
      expect(last?.inputs.params).toMatchObject({ name: 'Mum', body_color: '#222222' })
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

    await savePresetNamed(user, 'Nova tag')

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(create).toHaveBeenCalledWith('name-keychain', {
      name: 'Nova tag',
      inputs: { params: { name: 'Nova' } },
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
      inputs: { params: { name: 'Mummy', body_color: '#222222', text_color: '#FFFFFF' } },
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

  it('duplicates a preset the template ships into one of the user\'s own', async () => {
    const duplicate = vi.spyOn(api, 'duplicatePreset')
    const { user } = render()
    await user.selectOptions(await picker(), 'Tiny')

    await user.click(screen.getByRole('button', { name: 'Duplicate preset Tiny' }))
    const dialog = screen.getByRole('dialog', { name: 'Duplicate Tiny' })
    const field = within(dialog).getByRole('textbox', { name: 'Preset name' })
    expect(field).toHaveValue('Tiny copy')
    await user.clear(field)
    await user.type(field, 'Tiny for Bo')
    await user.click(within(dialog).getByRole('button', { name: 'Duplicate' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(duplicate).toHaveBeenCalledWith('name-keychain', 'template-tiny', { name: 'Tiny for Bo' })
    const select = screen.getByRole('combobox', { name: 'Preset' })
    expect(select).toHaveDisplayValue('Tiny for Bo')
    expect(within(select).getByRole('group', { name: 'Saved' })).toHaveTextContent('Tiny for Bo')
    // The copy is the user's, so it can be changed and deleted, unlike the original.
    await user.type(screen.getByRole('textbox', { name: 'Name on the tag' }), 'x')
    expect(screen.getByRole('button', { name: 'Update' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete preset Tiny for Bo' })).toBeInTheDocument()
  })

  it('keeps edits made before duplicating on screen, as a change to the copy', async () => {
    const { user } = render()
    await user.selectOptions(await picker(), 'Mum')
    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.clear(name)
    await user.type(name, 'Dad')

    await user.click(screen.getByRole('button', { name: 'Duplicate preset Mum' }))
    await user.click(
      within(screen.getByRole('dialog', { name: 'Duplicate Mum' })).getByRole('button', {
        name: 'Duplicate',
      }),
    )

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(name).toHaveValue('Dad')
    expect(screen.getByTestId('preset-modified')).toHaveTextContent('Changed from Mum copy')
  })

  it('offers no Duplicate until a preset is picked', async () => {
    render()
    await picker()
    expect(screen.queryByRole('button', { name: /^Duplicate preset/ })).not.toBeInTheDocument()
  })

  it('saves the UI state with the preset and hands it back on apply', async () => {
    let saved: unknown
    server.use(
      http.post('/api/v1/models/:slug/presets', async ({ request }) => {
        saved = await request.json()
        return HttpResponse.json(
          { id: 'e'.repeat(32), name: 'Lid', origin: 'mine', params: {}, inputs: { params: {}, tab: 'lid', v: 0 } },
          { status: 201 },
        )
      }),
    )
    const onApply = vi.fn()
    const { user } = renderPage(
      <PresetPicker slug="name-keychain" schema={keychainSchema} values={defaultValues(keychainSchema)} extra={{ tab: 'lid' }} onApply={onApply} />,
    )
    await savePresetNamed(user, 'Lid')
    await waitFor(() => expect(saved).toEqual({ name: 'Lid', inputs: { params: {}, tab: 'lid' } }))
    const select = await picker()
    await user.selectOptions(select, 'Tiny')
    await user.selectOptions(select, 'Lid')
    expect(onApply).toHaveBeenLastCalledWith(expect.anything(), { tab: 'lid', v: 0 })
  })

  it('Reset to defaults clears the UI state a preset brought', async () => {
    const lid = {
      id: 'f'.repeat(32),
      name: 'Lid',
      origin: 'mine',
      params: { name: 'Kai' },
      inputs: { params: { name: 'Kai' }, tab: 'lid', v: 0 },
    }
    const saved: { inputs: Record<string, unknown> }[] = []
    server.use(
      http.get('/api/v1/models/:slug/presets', () => HttpResponse.json([lid])),
      http.post('/api/v1/models/:slug/presets', async ({ request }) => {
        const body = (await request.json()) as { name: string; inputs: Record<string, unknown> }
        saved.push(body)
        return HttpResponse.json(
          { ...lid, id: String(saved.length).repeat(32), name: body.name, inputs: body.inputs },
          { status: 201 },
        )
      }),
    )
    const { user } = render()
    const select = await screen.findByRole('combobox', { name: 'Preset' })
    await waitFor(() => expect(within(select).getByRole('option', { name: 'Lid' })).toBeInTheDocument())
    await user.selectOptions(select, 'Lid')
    await savePresetNamed(user, 'With lid')
    await waitFor(() => expect(saved[0]?.inputs).toMatchObject({ tab: 'lid' }))
    const reset = screen.getByRole('button', { name: 'Reset to defaults' })
    await waitFor(() => expect(reset).toBeEnabled())
    await user.click(reset)
    await savePresetNamed(user, 'After reset')
    await waitFor(() => expect(saved).toHaveLength(2))
    expect(saved[1]?.inputs).not.toHaveProperty('tab')
  })
})
