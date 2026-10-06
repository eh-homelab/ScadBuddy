import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { bridge } from '../agent/bridge'
import { useGlobalAgentTools } from '../agent/global'
import { api } from '../api/client'
import type { Job } from '../api/types'
import { MAX_PRESET_DESCRIPTION } from '../lib/presets'
import { BUILTIN_SLUG, keychainSchema } from '../mocks/fixtures'
import { resetMockState, setMockPresets } from '../mocks/handlers'
import { emitRealtime } from '../mocks/realtime'
import { presets as fixturePresets } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { CustomizePage } from '../pages/CustomizePage'
import { renderPage } from '../test/utils'
import type { InputsExtra } from '../lib/inputs'
import { defaultValues, type ParamValues } from '../lib/params'
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

/** The picker with values that follow each apply, as the customize page's do. */
function Harness({ onApply }: { onApply: (values: ParamValues, extra: InputsExtra) => void }) {
  const [values, setValues] = useState(() => defaultValues(keychainSchema))
  const [extra, setExtra] = useState<InputsExtra>({ tab: 'lid' })
  return (
    <PresetPicker
      slug="name-keychain"
      schema={keychainSchema}
      values={values}
      extra={extra}
      onApply={(next, nextExtra) => {
        setValues(next)
        setExtra(nextExtra)
        onApply(next, nextExtra)
      }}
    />
  )
}

/** The app shell's agent tools (`fill`, `click`), which the page itself does not register. */
function AgentTools({ children }: { children: React.ReactNode }) {
  useGlobalAgentTools()
  return <>{children}</>
}

function presetId(select: HTMLElement, name: string): string {
  return (within(select).getByRole('option', { name }) as HTMLOptionElement).value
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

  it('asks before a pick replaces edits no preset holds (#359)', async () => {
    const { user } = render()
    const select = await picker()
    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.clear(name)
    await user.type(name, 'Emmalina')

    await user.selectOptions(select, 'Mum')
    const dialog = screen.getByRole('dialog', { name: 'Apply preset Mum?' })
    expect(dialog).toHaveTextContent('The values on screen have changes no preset holds.')
    expect(dialog).toHaveTextContent('To keep them, cancel and save them as a preset first.')
    // Nothing is replaced while it asks, and the picker still shows no preset.
    expect(name).toHaveValue('Emmalina')
    expect(select).toHaveValue('')

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(name).toHaveValue('Emmalina')

    // Escape closes it the same way, applying nothing.
    await user.selectOptions(select, 'Mum')
    expect(screen.getByRole('dialog', { name: 'Apply preset Mum?' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(name).toHaveValue('Emmalina')
    expect(select).toHaveValue('')

    await user.selectOptions(select, 'Mum')
    await user.click(
      within(screen.getByRole('dialog', { name: 'Apply preset Mum?' })).getByRole('button', {
        name: 'Replace my changes',
      }),
    )
    expect(name).toHaveValue('Mum')
    expect(select).toHaveDisplayValue('Mum')
  })

  it('asks before leaving a preset that was changed since it was picked (#359)', async () => {
    const { user } = render()
    const select = await picker()
    await user.selectOptions(select, 'Tiny')
    // Moving between unchanged presets loses nothing, so it does not ask.
    await user.selectOptions(select, 'Mum')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    await user.type(screen.getByRole('textbox', { name: 'Name on the tag' }), 'my')
    await user.selectOptions(select, 'Tiny')
    const dialog = screen.getByRole('dialog', { name: 'Apply preset Tiny?' })
    // The edits are changes to Mum, a saved preset, so Update is the way to keep them.
    expect(dialog).toHaveTextContent('You changed Mum since you picked it.')
    expect(dialog).toHaveTextContent('To keep them, cancel and update Mum or save them as a preset first.')
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Mummy')
  })

  it('keeps asking about the first preset picked while the dialog is open (#1475)', async () => {
    const { user } = render()
    const select = await picker()
    await user.type(screen.getByRole('textbox', { name: 'Name on the tag' }), 'x')

    fireEvent.change(select, { target: { value: presetId(select, 'Tiny') } })
    fireEvent.change(select, { target: { value: presetId(select, 'Mum') } })
    const dialog = screen.getByRole('dialog', { name: 'Apply preset Tiny?' })
    await user.click(within(dialog).getByRole('button', { name: 'Replace my changes' }))
    expect(select).toHaveDisplayValue('Tiny')
  })

  it('clears the selection without asking when "Choose a preset…" is picked over edits (#1475)', async () => {
    const { user } = render()
    const select = await picker()
    await user.selectOptions(select, 'Mum')
    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.type(name, 'my')

    await user.selectOptions(select, '')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(select).toHaveValue('')
    expect(name).toHaveValue('Mummy')
  })

  it("lets the assistant's fill apply a preset over edits once it confirms (#1445)", async () => {
    const { user } = renderPage(
      <AgentTools>
        <CustomizePage />
      </AgentTools>,
      { route: '/m/name-keychain', path: '/m/:slug' },
    )
    const select = await picker()
    await waitFor(() => expect(bridge.liveNames()).toContain('fill'))
    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.type(name, 'x')
    const edited = (name as HTMLInputElement).value

    // The pick asks first, and fill says so rather than returning the old value bare.
    const filled = await bridge.call('fill', { label: 'Preset', value: 'Mum' })
    expect(filled).toEqual({
      ok: true,
      result: {
        filled: 'Preset',
        value: '',
        confirm: expect.stringContaining('"Apply preset Mum?" opened instead') as unknown,
      },
    })
    expect(name).toHaveValue(edited)
    expect(select).toHaveValue('')

    // Replacing the values on screen stays in the page, so the assistant may confirm it.
    expect(await bridge.call('click', { role: 'button', name: 'Replace my changes' })).toMatchObject({ ok: true })
    expect(name).toHaveValue('Mum')
    expect(select).toHaveDisplayValue('Mum')
  })

  it('says which values it skipped because the template dropped them', async () => {
    const { user } = render()
    await user.selectOptions(await picker(), 'Old engraving')
    expect(screen.getByRole('status')).toHaveTextContent('engrave_depth')
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Ada')
  })

  it('shows each skipped value with what the preset stored (#358)', async () => {
    const { user } = render()
    await user.selectOptions(await picker(), 'Old engraving')
    expect(screen.getByRole('status')).toHaveTextContent('engrave_depth = 2')
  })

  it('asks before an Update drops the values this template no longer has (#358)', async () => {
    const update = vi.spyOn(api, 'updatePreset')
    const { user } = render()
    await user.selectOptions(await picker(), 'Old engraving')
    await user.type(screen.getByRole('textbox', { name: 'Name on the tag' }), 'm')
    await user.click(screen.getByRole('button', { name: 'Update' }))

    const dialog = screen.getByRole('dialog', { name: 'Update preset Old engraving' })
    expect(dialog).toHaveTextContent('engrave_depth = 2')
    expect(update).not.toHaveBeenCalled()

    // Cancel keeps the stored preset as it is.
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(update).not.toHaveBeenCalled()
    expect(screen.getByRole('status')).toHaveTextContent('engrave_depth')

    await user.click(screen.getByRole('button', { name: 'Update' }))
    await user.click(
      within(screen.getByRole('dialog', { name: 'Update preset Old engraving' })).getByRole('button', {
        name: 'Update and drop them',
      }),
    )
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('name-keychain', 'b1b2c3d4e5f60718293a4b5c6d7e8f90', {
        inputs: { params: { name: 'Adam' } },
      }),
    )
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
      description: '',
      tags: [],
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
    const name = within(dialog).getByRole('textbox', { name: 'Preset name' })
    await user.type(name, 'mum')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('A preset named "Mum" already exists.')
    // The server's reason is about the name, so the Name field is marked with it.
    expect(name).toHaveAttribute('aria-invalid', 'true')
    expect(name).toHaveAccessibleDescription(/already exists/)
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

  it('shows the picked preset\'s tags and description under the picker', async () => {
    const { user } = render()
    const select = await picker()
    expect(screen.queryByTestId('preset-details')).not.toBeInTheDocument()
    await user.selectOptions(select, 'Tiny')
    const details = screen.getByTestId('preset-details')
    const tags = within(details).getByRole('list', { name: 'Preset tags' })
    expect(within(tags).getAllByRole('listitem').map((t) => t.textContent)).toEqual([
      'small',
      'zip pull',
    ])
    // Markdown, rendered as elements: the bold is a <strong>, not asterisks.
    expect(within(details).getByText('zip pull', { selector: 'strong' })).toBeInTheDocument()
    // A preset with neither shows nothing.
    await user.selectOptions(select, 'Old engraving')
    expect(screen.queryByTestId('preset-details')).not.toBeInTheDocument()
  })

  it('saves a description and tags with a new preset', async () => {
    const create = vi.spyOn(api, 'createPreset')
    const { user } = render()
    await picker()
    await user.click(screen.getByRole('button', { name: 'Save as preset…' }))
    const dialog = screen.getByRole('dialog', { name: 'Save as preset' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Preset name' }), 'Bag tag')
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Description (optional, Markdown)' }),
      '  For **bags**. ',
    )
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Tags (optional, comma-separated)' }),
      'bags, Bags,  big  tag ,',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(create).toHaveBeenCalledWith('name-keychain', {
      name: 'Bag tag',
      inputs: { params: {} },
      description: 'For **bags**.',
      tags: ['bags', 'big tag'],
    })
    const details = screen.getByTestId('preset-details')
    expect(within(details).getAllByRole('listitem').map((t) => t.textContent)).toEqual([
      'bags',
      'big tag',
    ])
  })

  it('edits a saved preset\'s name and details, leaving its values alone', async () => {
    const update = vi.spyOn(api, 'updatePreset')
    const { user } = render()
    const select = await picker()
    await user.selectOptions(select, 'Mum')
    // An edit made first stays a change to the preset, for Update to save.
    await user.type(screen.getByRole('textbox', { name: 'Name on the tag' }), 'my')

    await user.click(screen.getByRole('button', { name: 'Edit details of preset Mum' }))
    const dialog = screen.getByRole('dialog', { name: 'Edit details of Mum' })
    const tags = within(dialog).getByRole('textbox', { name: 'Tags (optional, comma-separated)' })
    expect(tags).toHaveValue('gift')
    const name = within(dialog).getByRole('textbox', { name: 'Preset name' })
    await user.clear(name)
    await user.type(name, 'Mum (black)')
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Description (optional, Markdown)' }),
      'Black with white text.',
    )
    await user.clear(tags)
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(update).toHaveBeenCalledWith('name-keychain', 'a1b2c3d4e5f60718293a4b5c6d7e8f90', {
      name: 'Mum (black)',
      description: 'Black with white text.',
      tags: [],
    })
    expect(select).toHaveDisplayValue('Mum (black)')
    expect(screen.getByTestId('preset-details')).toHaveTextContent('Black with white text.')
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Mummy')
    expect(screen.getByTestId('preset-modified')).toHaveTextContent('Changed from Mum (black)')
  })

  it('says why tags are refused before sending them', async () => {
    const create = vi.spyOn(api, 'createPreset')
    const { user } = render()
    await picker()
    await user.click(screen.getByRole('button', { name: 'Save as preset…' }))
    const dialog = screen.getByRole('dialog', { name: 'Save as preset' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Preset name' }), 'Many')
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Tags (optional, comma-separated)' }),
      Array.from({ length: 21 }, (_, n) => `t${n}`).join(','),
    )
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent('At most 20 tags.')
    // The error is tied to the field it is about.
    const tags = within(dialog).getByRole('textbox', { name: 'Tags (optional, comma-separated)' })
    expect(tags).toHaveAttribute('aria-invalid', 'true')
    expect(tags).toHaveAccessibleDescription('At most 20 tags.')
    expect(tags).toHaveFocus()
    expect(create).not.toHaveBeenCalled()
  })

  it('says why a description is refused before sending it, counting as the server does', async () => {
    const create = vi.spyOn(api, 'createPreset')
    const { user } = render()
    await picker()
    await user.click(screen.getByRole('button', { name: 'Save as preset…' }))
    const dialog = screen.getByRole('dialog', { name: 'Save as preset' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Preset name' }), 'Long')
    const description = within(dialog).getByRole('textbox', {
      name: 'Description (optional, Markdown)',
    })
    // No `maxLength` on the field: it would count UTF-16 units, cutting an emoji short
    // of the server's bound, which is in code points, as the Tags field counts.
    expect(description).not.toHaveAttribute('maxlength')
    await user.click(description)
    await user.paste('\u{1F600}'.repeat(MAX_PRESET_DESCRIPTION))
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(create).toHaveBeenCalledOnce())
    expect(create.mock.calls[0]?.[1]?.description).toBe(
      '\u{1F600}'.repeat(MAX_PRESET_DESCRIPTION),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Save as preset…' }))
    const again = screen.getByRole('dialog', { name: 'Save as preset' })
    await user.type(within(again).getByRole('textbox', { name: 'Preset name' }), 'Longer')
    const tooLong = within(again).getByRole('textbox', { name: 'Description (optional, Markdown)' })
    await user.click(tooLong)
    await user.paste('d'.repeat(MAX_PRESET_DESCRIPTION + 1))
    await user.click(within(again).getByRole('button', { name: 'Save' }))
    const alert = await within(again).findByRole('alert')
    expect(alert).toHaveTextContent(
      `The description is longer than ${MAX_PRESET_DESCRIPTION} characters.`,
    )
    expect(tooLong).toHaveAttribute('aria-invalid', 'true')
    expect(tooLong).toHaveAccessibleDescription(/longer than/)
    expect(tooLong).toHaveFocus()
    expect(create).toHaveBeenCalledOnce()
  })

  it('leaves the tags out of an Edit details save that did not touch them', async () => {
    const update = vi.spyOn(api, 'updatePreset')
    const { user } = render()
    const select = await picker()
    await user.selectOptions(select, 'Mum')
    await user.click(screen.getByRole('button', { name: 'Edit details of preset Mum' }))
    const dialog = screen.getByRole('dialog', { name: 'Edit details of Mum' })
    const name = within(dialog).getByRole('textbox', { name: 'Preset name' })
    await user.clear(name)
    await user.type(name, 'Mother')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(update).toHaveBeenCalledWith('name-keychain', 'a1b2c3d4e5f60718293a4b5c6d7e8f90', {
      name: 'Mother',
      description: '',
    })
  })

  it('offers Edit details only on a saved preset', async () => {
    const { user } = render()
    await user.selectOptions(await picker(), 'Tiny')
    expect(screen.queryByRole('button', { name: /^Edit details of preset/ })).not.toBeInTheDocument()
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
          {
            id: 'e'.repeat(32),
            name: 'Lid',
            origin: 'mine',
            params: {},
            inputs: { params: {}, tab: 'lid', v: 0 },
            description: '',
            tags: [],
          },
          { status: 201 },
        )
      }),
    )
    const onApply = vi.fn()
    const { user } = renderPage(<Harness onApply={onApply} />)
    await savePresetNamed(user, 'Lid')
    await waitFor(() => expect(saved).toEqual({ name: 'Lid', inputs: { params: {}, tab: 'lid' }, description: '', tags: [] }))
    const select = await picker()
    // Away by way of another preset and back: the harness's values follow each pick.
    await user.selectOptions(select, 'Tiny')
    await user.selectOptions(select, 'Lid')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(onApply).toHaveBeenLastCalledWith(expect.anything(), { tab: 'lid', v: 0 })
  })

  it('follows a preset deleted in another tab (#357)', async () => {
    const { user } = render()
    const select = await picker()
    await user.selectOptions(select, 'Mum')
    // Another tab deletes it: the server says the template's presets changed.
    setMockPresets(
      'name-keychain',
      (fixturePresets['name-keychain'] ?? []).filter((preset) => preset.name !== 'Mum'),
    )
    emitRealtime('presets.changed', ['model:name-keychain'], { slug: 'name-keychain' })

    await waitFor(() => expect(within(select).queryByRole('option', { name: 'Mum' })).toBeNull())
    expect(select).toHaveValue('')
    expect(screen.getByRole('alert')).toHaveTextContent('That preset was deleted elsewhere.')
    // The values it put on screen stay.
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Mum')
  })

  it('lists a preset saved elsewhere, the assistant\'s included (#357)', async () => {
    render()
    const select = await picker()
    setMockPresets('name-keychain', [
      ...(fixturePresets['name-keychain'] ?? []),
      { id: 'c'.repeat(32), name: 'From the assistant', origin: 'mine', params: {}, description: '', tags: [] },
    ])
    emitRealtime('presets.changed', ['model:name-keychain'], { slug: 'name-keychain' })
    expect(await within(select).findByRole('option', { name: 'From the assistant' })).toBeInTheDocument()
  })

  it.each(['Update', 'Delete'])(
    'drops a preset that %s finds deleted elsewhere, without its id (#357)',
    async (action) => {
      const { user } = render()
      const select = await picker()
      await user.selectOptions(select, 'Mum')
      // Gone on the server, and this tab was not told.
      setMockPresets(
        'name-keychain',
        (fixturePresets['name-keychain'] ?? []).filter((preset) => preset.name !== 'Mum'),
      )
      if (action === 'Update') {
        await user.type(screen.getByRole('textbox', { name: 'Name on the tag' }), 'my')
        await user.click(screen.getByRole('button', { name: 'Update' }))
      } else {
        await user.click(screen.getByRole('button', { name: 'Delete preset Mum' }))
        await user.click(
          within(screen.getByRole('dialog', { name: 'Delete preset Mum' })).getByRole('button', {
            name: 'Delete preset',
          }),
        )
      }

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent('That preset was deleted elsewhere.')
      expect(alert).not.toHaveTextContent('a1b2c3d4')
      expect(within(select).queryByRole('option', { name: 'Mum' })).toBeNull()
      expect(select).toHaveValue('')
    },
  )

  it('Reset to defaults clears the UI state a preset brought', async () => {
    const lid = {
      id: 'f'.repeat(32),
      name: 'Lid',
      origin: 'mine',
      params: { name: 'Kai' },
      inputs: { params: { name: 'Kai' }, tab: 'lid', v: 0 },
      description: '',
      tags: [],
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
