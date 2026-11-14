import { fireEvent, screen, within } from '@testing-library/react'
import { useState, type ComponentProps } from 'react'
import { describe, expect, it } from 'vitest'
import type { CustomizerSchema, Filament, Param } from '../../api/types'
import { keychainSchema } from '../../mocks/fixtures'
import { renderPage } from '../../test/utils'
import { ListingEditor } from './ListingEditor'
import { defaultsFor, emptyDraft, listingInput, previewListing, unsaveable, type ListingDraft } from './listingDraft'

const COMMIT = 'a'.repeat(40)

function filament(id: string, colour: string, colour_name: string): Filament {
  return {
    id,
    material: 'PLA',
    subtype: null,
    brand: null,
    colour_name,
    colour,
    alpha: 255,
    extra_colours: [],
    effect: null,
    colour_source: 'manual',
    cost_per_kg: '20',
    density_g_cm3: '1.24',
    density_profile: null,
    archived_at: null,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
  }
}

const RED = filament('f1', '#FF0000', 'Red')
const BLUE = filament('f2', '#1B6CA8', 'Ocean')
const CORAL = filament('f3', '#E8532F', 'Coral')

function Harness({ seen, schema = keychainSchema, filaments = [RED, BLUE, CORAL] }: { seen: ListingDraft[]; schema?: CustomizerSchema; filaments?: Filament[] }) {
  const [draft, setDraft] = useState<ListingDraft>({ ...emptyDraft('name-keychain', COMMIT), title: 'Tag', ...defaultsFor(schema, filaments) })
  return (
    <ListingEditor
      draft={draft}
      onChange={(next) => {
        seen.push(next)
        setDraft(next)
      }}
      models={[]}
      versions={[]}
      schema={schema}
      filaments={filaments}
      media={[]}
    />
  )
}

function last(seen: ListingDraft[]): ListingDraft {
  const draft = seen.at(-1)
  if (!draft) throw new Error('no change was made')
  return draft
}

describe('ListingEditor (#5282, #6131)', () => {
  it('puts what the customer chooses apart from what the shop sets, folded to one line', async () => {
    const { user } = renderPage(<Harness seen={[]} />)
    const chooses = screen.getByRole('region', { name: 'What the customer chooses' })
    expect(within(chooses).getAllByTestId('listing-param').map((row) => row.querySelector('[title]')?.textContent)).toEqual(['Name on the tag', 'Plate', 'Text'])
    const set = screen.getByRole('region', { name: 'Set by you' })
    expect(set).toHaveTextContent('Text size 14, ')
    expect(within(set).queryByRole('group', { name: 'Text size' })).toBeNull()
    await user.click(within(set).getByRole('button', { name: 'Show all 8' }))
    expect(within(set).getByRole('group', { name: 'Text size' })).toBeInTheDocument()
    // No variable names beside the captions.
    expect(screen.queryByText('text_size')).not.toBeInTheDocument()
  })

  it('lets the customer change a locked parameter, and narrows its bounds', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness seen={seen} />)
    await user.click(screen.getByRole('button', { name: /^Show all/ }))
    await user.click(screen.getByRole('switch', { name: 'Customer can change Margin around the text' }))
    expect(last(seen).params['padding']).toEqual({ kind: 'number', min: 6, max: 6, step: null })
    expect(last(seen).fixed['padding']).toBeUndefined()
    fireEvent.change(screen.getByLabelText('To Margin around the text'), { target: { value: '12' } })
    expect(last(seen).params['padding']).toMatchObject({ max: 12 })

    await user.click(screen.getByRole('switch', { name: 'Customer can change Hole position' }))
    await user.click(screen.getByRole('checkbox', { name: 'Offer Top centre' }))
    expect(last(seen).params['hole_side']).toEqual({ kind: 'choice', choices: ['left', 'right'] })

    await user.click(screen.getByRole('switch', { name: 'Customer can change Name on the tag' }))
    expect(last(seen).params['name']).toBeUndefined()
    expect(last(seen).fixed['name']).toBe('Reagan')
  })

  it('says why a typeface is never the customer’s', async () => {
    const { user } = renderPage(<Harness seen={[]} />)
    await user.click(screen.getByRole('button', { name: /^Show all/ }))
    expect(screen.getByRole('switch', { name: 'Customer can change Typeface' })).toBeDisabled()
    expect(screen.getByText('A customer cannot choose a typeface.')).toBeInTheDocument()
  })

  it('never offers a support setting, and says why', async () => {
    const schema: CustomizerSchema = {
      ...keychainSchema,
      parameters: [...(keychainSchema.parameters ?? []), { name: 'support_colour', type: 'color', group: 'Supports', caption: 'Support material', initial: '#E8532F' }],
    }
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness seen={seen} schema={schema} />)
    await user.click(screen.getByRole('button', { name: /^Show all/ }))
    expect(screen.getByRole('switch', { name: 'Customer can change Support material' })).toBeDisabled()
    expect(screen.getByText('Support settings are yours: a customer never sees them.')).toBeInTheDocument()
  })

  it('marks the filament a support colour alone uses as not offered (#6140 review)', () => {
    const schema: CustomizerSchema = {
      ...keychainSchema,
      parameters: [...(keychainSchema.parameters ?? []), { name: 'support_colour', type: 'color', group: 'Supports', caption: 'Support material', initial: '#F5F5DC' }],
    }
    const pva = { ...filament('f-pva', '#F5F5DC', 'Natural'), material: 'PVA' }
    renderPage(<Harness seen={[]} schema={schema} filaments={[RED, BLUE, CORAL, pva]} />)
    const colours = screen.getByRole('group', { name: 'Colours on offer' })
    // The smart defaults put it on the listing, so publish takes the locked support colour.
    expect(within(colours).getByRole('checkbox', { name: 'PVA Natural' })).toBeChecked()
    expect(within(colours).getByText('PVA Natural').closest('label')).toHaveTextContent('(for supports only, not offered)')
    expect(within(colours).getByText('PLA Ocean').closest('label')).not.toHaveTextContent('for supports only')
  })

  it('does not offer a colour while the only ticked filament is for supports only (#6140 review)', async () => {
    const schema: CustomizerSchema = {
      ...keychainSchema,
      parameters: [...(keychainSchema.parameters ?? []), { name: 'support_colour', type: 'color', group: 'Supports', caption: 'Support material', initial: '#F5F5DC' }],
    }
    const pva = { ...filament('f-pva', '#F5F5DC', 'Natural'), material: 'PVA' }
    const draft: ListingDraft = { ...emptyDraft('name-keychain', COMMIT), title: 'Tag', colours: [pva.id], fixed: { body_color: '#1B6CA8', text_color: '#E8532F' } }
    const { user } = renderPage(<ListingEditor draft={draft} onChange={() => undefined} models={[]} versions={[]} schema={schema} filaments={[RED, pva]} media={[]} />)
    await user.click(screen.getByRole('button', { name: /^Show all/ }))
    expect(screen.getByRole('switch', { name: 'Customer can change Plate' })).toBeDisabled()
    expect(screen.getAllByText('Tick a colour on offer first.').length).toBeGreaterThan(0)
  })

  it('names an offered parameter with nothing to choose by its caption', () => {
    const body = listingInput({ ...emptyDraft('name-keychain', COMMIT), title: 'Tag', params: { body_color: { kind: 'choice', choices: [] } } }, keychainSchema, [])
    expect(unsaveable(body, keychainSchema)).toMatch(/^“Plate” is offered with nothing to choose\./)
  })

  it('gives an offered colour the listing filaments’ colours when it is saved', () => {
    const draft: ListingDraft = {
      ...emptyDraft('name-keychain', COMMIT),
      title: 'Tag',
      colours: [RED.id],
      params: { body_color: { kind: 'choice', choices: [] } },
    }
    expect(listingInput(draft, keychainSchema, [RED]).params).toEqual({ body_color: { kind: 'choice', choices: ['#FF0000'] } })
  })

  it('names a colour no filament has, and uses the filament chosen instead', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness seen={seen} filaments={[RED, CORAL]} />)
    const problems = screen.getByTestId('colour-problems')
    expect(problems).toHaveTextContent('Plate is #1B6CA8')
    expect(within(problems).getByRole('link', { name: 'add one to the price book' })).toHaveAttribute('href', '/settings/printing#orders')
    await user.click(within(problems).getByRole('combobox', { name: 'Filament for Plate' }))
    await user.click(await screen.findByRole('option', { name: 'PLA Red' }))
    expect(last(seen).fixed['body_color']).toBe('#FF0000')
    expect(last(seen).colours).toContain(RED.id)
    expect(screen.queryByTestId('colour-problems')).not.toBeInTheDocument()
  })

  it('says to leave a colour the customer chooses out of the title, and takes it out', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness seen={seen} />)
    // The keychain's default colours are on offer: Ocean and Coral.
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Name tag – Ocean' } })
    expect(screen.getByTestId('title-colours')).toHaveTextContent('Leave Ocean out of the title')
    await user.click(screen.getByRole('button', { name: 'Take it out' }))
    expect(last(seen).title).toBe('Name tag')
    expect(screen.queryByTestId('title-colours')).not.toBeInTheDocument()
  })

  it('folds the design and version pickers back to one line with Done (#6140 review)', async () => {
    const versions = [{ commit: COMMIT, short: 'aaaaaaa', author: 'a', date: '2026-11-01T00:00:00Z', message: 'm', files: [], current: true }]
    const models = [{ slug: 'name-keychain', name: 'Name Keychain' }] as unknown as ComponentProps<typeof ListingEditor>['models']
    const draft: ListingDraft = { ...emptyDraft('name-keychain', COMMIT), title: 'Tag' }
    const { user } = renderPage(<ListingEditor draft={draft} onChange={() => undefined} models={models} versions={versions} schema={keychainSchema} filaments={[]} media={[]} />)
    expect(screen.getByTestId('listing-design')).toHaveTextContent('version aaaaaaa (current)')
    await user.click(screen.getByRole('button', { name: 'Change' }))
    expect(screen.getByRole('combobox', { name: 'Version' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.getByTestId('listing-design')).toBeInTheDocument()
  })

  it('keeps an offered colour’s own label and default when it is saved (#6137, #6144)', () => {
    const draft: ListingDraft = {
      ...emptyDraft('name-keychain', COMMIT),
      title: 'Tag',
      colours: [RED.id],
      params: { body_color: { kind: 'choice', choices: [], label: 'Colour of the letters', default: '#FF0000' } },
    }
    expect(listingInput(draft, keychainSchema, [RED]).params).toEqual({
      body_color: { kind: 'choice', choices: ['#FF0000'], label: 'Colour of the letters', default: '#FF0000' },
    })
  })

  it('previews each label and placeholder as the public page shows them (#6137)', () => {
    const schema: CustomizerSchema = {
      ...keychainSchema,
      parameters: [
        { ...keychainSchema.parameters!.find((p) => p.name === 'body_color')!, caption: 'Letter colour (extruder 2)' },
        { name: 'line', type: 'string', initial: 'Your name', caption: 'Name', group: 'Text', max_length: 20 } as Param,
        { name: 'size', type: 'slider', initial: 40, caption: 'Size', group: 'Plate', min: 20, max: 80, step: 1 } as Param,
      ],
    }
    const draft: ListingDraft = {
      ...emptyDraft('name-keychain', COMMIT),
      title: 'Tag',
      colours: [RED.id],
      params: {
        body_color: { kind: 'choice', choices: [] },
        line: { kind: 'text', max_length: 20 },
        size: { kind: 'number', min: 20, max: 80, step: 1, label: 'How big' },
      },
    }
    const shown = previewListing(draft, schema, [RED], [], () => '').params
    expect(shown.body_color!.label).toBe('Letter colour')
    expect(shown.size!.label).toBe('How big')
    expect(shown.line).toMatchObject({ label: 'Name', placeholder: 'Your name' })
  })

  it('previews the default an unpicked render takes, as the public page does (#6144)', () => {
    const schema: CustomizerSchema = {
      ...keychainSchema,
      parameters: [
        { ...keychainSchema.parameters!.find((p) => p.name === 'body_color')!, initial: '#ff0000' },
        { name: 'shape', type: 'select', initial: 'round', caption: 'Shape', group: 'Plate', options: [{ name: 'round', value: 'round' }, { name: 'square', value: 'square' }] } as Param,
        { name: 'size', type: 'slider', initial: 40, caption: 'Size', group: 'Plate', min: 20, max: 80, step: 1 } as Param,
        { name: 'line', type: 'string', initial: 'Your name', caption: 'Name', group: 'Text', max_length: 20 } as Param,
      ],
    }
    const draft: ListingDraft = {
      ...emptyDraft('name-keychain', COMMIT),
      title: 'Tag',
      colours: [RED.id],
      params: {
        body_color: { kind: 'choice', choices: [] },
        shape: { kind: 'choice', choices: ['round', 'square'] },
        size: { kind: 'number', min: 20, max: 80, step: 1 },
        line: { kind: 'text', max_length: 20, default: 'Ada' },
      },
    }
    const shown = previewListing(draft, schema, [RED], [], () => '').params
    expect(shown.body_color!.default).toBe('#ff0000')
    expect(shown.shape!.default).toBe('round')
    expect(shown.size!.default).toBe(40)
    expect(shown.line).toMatchObject({ default: 'Ada', placeholder: 'Ada' })
    const elsewhere = previewListing({ ...draft, colours: [] }, schema, [RED], [], () => '').params
    expect(elsewhere.body_color!.default).toBeNull()
    // A choice default outside its choices (a listing saved before #6144) shows none.
    const stale = previewListing({ ...draft, params: { ...draft.params, shape: { kind: 'choice', choices: ['square'] } } }, schema, [RED], [], () => '').params
    expect(stale.shape!.default).toBeNull()
  })

  it('previews no number default outside its range or off its step, as the public page does (#6144)', () => {
    const schema: CustomizerSchema = {
      ...keychainSchema,
      parameters: [{ name: 'size', type: 'slider', initial: 50, caption: 'Size', group: 'Plate', min: 20, max: 80, step: 1 } as Param],
    }
    const draft = (size: ListingDraft['params'][string]): ListingDraft => ({ ...emptyDraft('name-keychain', COMMIT), title: 'Tag', params: { size } })
    const shown = (size: ListingDraft['params'][string]) => previewListing(draft(size), schema, [], [], () => '').params.size!.default
    // A listing saved before #6144: the template's 50 on a 60 to 80 field.
    expect(shown({ kind: 'number', min: 60, max: 80, step: 1 })).toBeNull()
    expect(shown({ kind: 'number', min: 20, max: 80, step: 1, default: 85 })).toBeNull()
    expect(shown({ kind: 'number', min: 20, max: 80, step: 1, default: 30.5 })).toBeNull()
    // With no step of its own, the schema's applies, as the backend checks it.
    expect(shown({ kind: 'number', min: 20, max: 80, default: 30.5 })).toBeNull()
    expect(shown({ kind: 'number', min: 20, max: 80, step: 1, default: 30 })).toBe(30)
  })

  it('locks a numeric select at one of its options, as a number', async () => {
    const seen: ListingDraft[] = []
    const holeSide = (keychainSchema.parameters ?? []).find((p) => p.name === 'hole_side') as Param
    const lines: Param = {
      ...holeSide,
      name: 'lines',
      caption: 'Lines',
      initial: 1,
      options: [
        { name: 'One', value: 1 },
        { name: 'Two', value: 2 },
      ],
    }
    const { user } = renderPage(<Harness seen={seen} schema={{ ...keychainSchema, parameters: [...(keychainSchema.parameters ?? []), lines] }} />)
    await user.click(screen.getByRole('button', { name: /^Show all/ }))
    await user.click(screen.getByRole('combobox', { name: 'Value of Lines' }))
    await user.click(await screen.findByRole('option', { name: 'Two' }))
    expect(last(seen).fixed['lines']).toBe(2)
  })

  it('locks a switch on or off', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness seen={seen} />)
    await user.click(screen.getByRole('button', { name: /^Show all/ }))
    const value = screen.getByRole('switch', { name: 'Value of Keyring hole' })
    expect(value).toBeChecked()
    await user.click(value)
    expect(last(seen).fixed['keyring_hole']).toBe(false)
  })
})
