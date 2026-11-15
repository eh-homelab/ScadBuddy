import { fireEvent, screen, within } from '@testing-library/react'
import { useState, type ComponentProps } from 'react'
import { describe, expect, it } from 'vitest'
import type { CustomizerSchema, Filament, Param, StorefrontListing } from '../../api/types'
import { keychainSchema } from '../../mocks/fixtures'
import { renderPage } from '../../test/utils'
import { ListingEditor } from './ListingEditor'
import { ListingView } from '../../storefront/components/ListingView'
import { defaultsFor, draftOf, emptyDraft, listingInput, previewListing, shownParams, unsaveable, type ListingDraft } from './listingDraft'

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
    spool_linked: false,
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
  it('lists every setting once, in the design’s order, and a switch never moves it (#6290)', async () => {
    const { user } = renderPage(<Harness seen={[]} />)
    const settings = screen.getByRole('region', { name: 'Settings' })
    const names = () => within(settings).getAllByTestId('listing-param').map((row) => row.querySelector('[title]')?.textContent)
    const order = shownParams(keychainSchema).map((param) => param.caption || param.name)
    expect(names()).toEqual(order)
    // A locked setting shows its value in place; no section hides it.
    expect(within(settings).getByRole('group', { name: 'Text size' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Set by you' })).toBeNull()

    const at = order.indexOf('Name on the tag')
    await user.click(screen.getByRole('switch', { name: 'Customer can change Name on the tag' }))
    expect(names()).toEqual(order)
    expect(within(within(settings).getAllByTestId('listing-param')[at]!).getByLabelText('Value of Name on the tag')).toHaveValue('Reagan')
    await user.click(screen.getByRole('switch', { name: 'Customer can change Name on the tag' }))
    expect(names()).toEqual(order)
    expect(within(within(settings).getAllByTestId('listing-param')[at]!).queryByLabelText('Value of Name on the tag')).toBeNull()
    // No variable names beside the captions.
    expect(screen.queryByText('text_size')).not.toBeInTheDocument()
  })

  it('folds the colours on offer to a count after the settings, and opens them (#6290)', async () => {
    const { user } = renderPage(<Harness seen={[]} />)
    const colours = screen.getByRole('group', { name: 'Colours on offer' })
    expect(within(colours).queryByRole('list')).toBeNull()
    expect(colours).toHaveTextContent(/: \d+ ticked \(/)
    await user.click(within(colours).getByRole('button', { name: 'Show all 3 filaments' }))
    expect(within(colours).getAllByRole('listitem')).toHaveLength(3)
    // After the settings in the page.
    const settings = screen.getByRole('region', { name: 'Settings' })
    expect(settings.compareDocumentPosition(colours) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('opens the colours on offer when none is ticked, and keeps them open while colours are ticked (#6292 review)', async () => {
    const seen: ListingDraft[] = []
    function Untouched() {
      const [draft, setDraft] = useState<ListingDraft>({ ...emptyDraft('name-keychain', COMMIT), title: 'Tag' })
      return (
        <ListingEditor
          draft={draft}
          onChange={(next) => {
            seen.push(next)
            setDraft(next)
          }}
          models={[]}
          versions={[]}
          schema={keychainSchema}
          filaments={[RED, BLUE, CORAL]}
          media={[]}
        />
      )
    }
    const { user } = renderPage(<Untouched />)
    const colours = screen.getByRole('group', { name: 'Colours on offer' })
    expect(within(colours).getAllByRole('listitem')).toHaveLength(3)
    await user.click(within(colours).getByRole('checkbox', { name: 'PLA Red' }))
    await user.click(within(colours).getByRole('checkbox', { name: 'PLA Ocean' }))
    // Still open under the person's hand.
    expect(within(colours).getAllByRole('listitem')).toHaveLength(3)
    expect(last(seen).colours).toEqual(['f1', 'f2'])
  })

  it('decides whether the colours start open only once the listing has loaded (#6292 review)', () => {
    const draft: ListingDraft = { ...emptyDraft('name-keychain', COMMIT), title: 'Tag' }
    const props = { onChange: () => undefined, models: [], versions: [], schema: keychainSchema, media: [] }
    // Loading: no filaments yet, so it says so rather than "no filaments".
    const { rerender } = renderPage(<ListingEditor {...props} draft={draft} filaments={[]} coloursSettled={false} bookState="loading" />)
    expect(screen.getByRole('group', { name: 'Colours on offer' })).toHaveTextContent('Reading the price book…')
    // The book and the defaults land: ticked, so it folds.
    rerender(<ListingEditor {...props} draft={{ ...draft, colours: ['f1'] }} filaments={[RED, BLUE]} coloursSettled={true} />)
    const colours = screen.getByRole('group', { name: 'Colours on offer' })
    expect(within(colours).queryByRole('list')).toBeNull()
    expect(colours).toHaveTextContent('1 ticked (PLA Red)')
  })

  it('lets the customer change a locked parameter, and narrows its bounds', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness seen={seen} />)
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

  it('starts an offered number at the template’s value, and moves it inside a range that leaves it out (#6144)', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness seen={seen} />)
    await user.click(screen.getByRole('switch', { name: 'Customer can change Text size' }))
    expect(last(seen).params['text_size']).toEqual({ kind: 'number', min: 6, max: 28, step: 0.5 })
    expect(screen.getByLabelText('Default of Text size')).toHaveValue(14)

    fireEvent.change(screen.getByLabelText('From Text size'), { target: { value: '16' } })
    expect(last(seen).params['text_size']).toEqual({ kind: 'number', min: 16, max: 28, step: 0.5, default: 16 })
    fireEvent.change(screen.getByLabelText('Default of Text size'), { target: { value: '20' } })
    expect(last(seen).params['text_size']).toMatchObject({ default: 20 })

    // The range takes the template's value again: the listing's own stays until the person goes back.
    fireEvent.change(screen.getByLabelText('From Text size'), { target: { value: '6' } })
    expect(last(seen).params['text_size']).toMatchObject({ min: 6, default: 20 })
    await user.click(screen.getByRole('button', { name: 'Use the template’s (14)' }))
    expect(last(seen).params['text_size']).toEqual({ kind: 'number', min: 6, max: 28, step: 0.5 })
  })

  it('refits a starting value from the person’s choice as a bound is typed, never from a half-typed one (#6402 review)', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness seen={seen} />)
    // "12" passes through "1", which leaves out the template's "Reagan"; "12" takes it again.
    // One change per keystroke, as typing into the field sends them.
    for (const value of ['1', '12']) fireEvent.change(screen.getByLabelText('Most characters'), { target: { value } })
    expect(seen.map((draft) => draft.params['name']?.default)).toContain('R')
    expect(last(seen).params['name']).toEqual({ kind: 'text', max_length: 12 })

    // The person's own starting value comes back once the range takes it again.
    await user.click(screen.getByRole('switch', { name: 'Customer can change Text size' }))
    fireEvent.change(screen.getByLabelText('Default of Text size'), { target: { value: '20' } })
    for (const value of ['2', '25']) fireEvent.change(screen.getByLabelText('To Text size'), { target: { value } })
    expect(last(seen).params['text_size']).toEqual({ kind: 'number', min: 6, max: 25, step: 0.5, default: 20 })
  })

  it('names a starting value the bounds leave out (#6144)', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness seen={seen} />)
    await user.click(screen.getByRole('switch', { name: 'Customer can change Text size' }))
    fireEvent.change(screen.getByLabelText('Default of Text size'), { target: { value: '14.2' } })
    expect(screen.getByText('A customer who picks nothing gets 14.2, which these choices do not allow. Choose another starting value.')).toBeInTheDocument()
  })

  it('starts a choice the person narrowed at one it still offers (#6144)', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness seen={seen} />)
    await user.click(screen.getByRole('switch', { name: 'Customer can change Hole position' }))
    await user.click(screen.getByRole('checkbox', { name: 'Offer Left' }))
    expect(last(seen).params['hole_side']).toEqual({ kind: 'choice', choices: ['right', 'top'], default: 'right' })
  })

  it('starts a colour at a colour on offer when none has the template’s (#6144)', () => {
    renderPage(<Harness seen={[]} filaments={[RED, BLUE]} />)
    expect(within(screen.getByRole('group', { name: 'Text' })).getByLabelText('Default of Text')).toHaveTextContent('PLA Ocean')
  })

  it('says why a typeface is never the customer’s', async () => {
    renderPage(<Harness seen={[]} />)
    expect(screen.getByRole('switch', { name: 'Customer can change Typeface' })).toBeDisabled()
    expect(screen.getByText('A customer cannot choose a typeface.')).toBeInTheDocument()
  })

  it('never offers a support setting, and says why', async () => {
    const schema: CustomizerSchema = {
      ...keychainSchema,
      parameters: [...(keychainSchema.parameters ?? []), { name: 'support_colour', type: 'color', group: 'Supports', caption: 'Support material', initial: '#E8532F' }],
    }
    const seen: ListingDraft[] = []
    renderPage(<Harness seen={seen} schema={schema} />)
    expect(screen.getByRole('switch', { name: 'Customer can change Support material' })).toBeDisabled()
    expect(screen.getByText('Support settings are yours: a customer never sees them.')).toBeInTheDocument()
  })

  it('marks the filament a support colour alone uses as not offered (#6140 review)', async () => {
    const schema: CustomizerSchema = {
      ...keychainSchema,
      parameters: [...(keychainSchema.parameters ?? []), { name: 'support_colour', type: 'color', group: 'Supports', caption: 'Support material', initial: '#F5F5DC' }],
    }
    const pva = { ...filament('f-pva', '#F5F5DC', 'Natural'), material: 'PVA' }
    const { user } = renderPage(<Harness seen={[]} schema={schema} filaments={[RED, BLUE, CORAL, pva]} />)
    const colours = screen.getByRole('group', { name: 'Colours on offer' })
    await user.click(within(colours).getByRole('button', { name: /^Show all/ }))
    // The smart defaults put it on the listing, so publish takes the locked support colour.
    expect(within(colours).getByRole('checkbox', { name: 'PVA Natural' })).toBeChecked()
    expect(within(colours).getByText('PVA Natural').closest('label')).toHaveTextContent('(for supports only, not offered)')
    expect(within(colours).getByRole('checkbox', { name: 'PLA Ocean' }).closest('label')).not.toHaveTextContent('for supports only')
  })

  it('marks a support-only filament in the folded names too (#6292 review)', () => {
    const schema: CustomizerSchema = {
      ...keychainSchema,
      parameters: [...(keychainSchema.parameters ?? []), { name: 'support_colour', type: 'color', group: 'Supports', caption: 'Support material', initial: '#F5F5DC' }],
    }
    const pva = { ...filament('f-pva', '#F5F5DC', 'Natural'), material: 'PVA' }
    renderPage(<Harness seen={[]} schema={schema} filaments={[RED, BLUE, CORAL, pva]} />)
    expect(screen.getByRole('group', { name: 'Colours on offer' })).toHaveTextContent('PVA Natural, for supports only')
  })

  it('does not offer a colour while the only ticked filament is for supports only (#6140 review)', async () => {
    const schema: CustomizerSchema = {
      ...keychainSchema,
      parameters: [...(keychainSchema.parameters ?? []), { name: 'support_colour', type: 'color', group: 'Supports', caption: 'Support material', initial: '#F5F5DC' }],
    }
    const pva = { ...filament('f-pva', '#F5F5DC', 'Natural'), material: 'PVA' }
    const draft: ListingDraft = { ...emptyDraft('name-keychain', COMMIT), title: 'Tag', colours: [pva.id], fixed: { body_color: '#1B6CA8', text_color: '#E8532F' } }
    renderPage(<ListingEditor draft={draft} onChange={() => undefined} models={[]} versions={[]} schema={schema} filaments={[RED, pva]} media={[]} />)
    expect(screen.getByRole('switch', { name: 'Customer can change Plate' })).toBeDisabled()
    expect(screen.getAllByText('Tick a colour on offer below first.').length).toBeGreaterThan(0)
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
    // Offered, the colour starts at one on offer (#6144); locked at the template's, it has no filament.
    expect(screen.queryByTestId('colour-problems')).not.toBeInTheDocument()
    await user.click(screen.getByRole('switch', { name: 'Customer can change Plate' }))
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
    await user.click(screen.getByRole('combobox', { name: 'Value of Lines' }))
    await user.click(await screen.findByRole('option', { name: 'Two' }))
    expect(last(seen).fixed['lines']).toBe(2)
  })

  it('locks a switch on or off', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness seen={seen} />)
    const value = screen.getByRole('switch', { name: 'Value of Keyring hole' })
    expect(value).toBeChecked()
    await user.click(value)
    expect(last(seen).fixed['keyring_hole']).toBe(false)
  })
})

describe('ListingEditor materials and colours on offer (#6258)', () => {
  const PETG_BLACK = { ...filament('g1', '#1E1E1E', 'Black'), material: 'PETG' }
  const PETG_CLEAR = { ...filament('g2', '#EEF3F4', 'Clear'), material: 'PETG' }
  const ABS_GREY = { ...filament('a1', '#7D7F7C', 'Grey'), material: 'ABS' }
  const ALL = [RED, BLUE, PETG_BLACK, PETG_CLEAR, ABS_GREY]
  const SHOP = ['PETG', 'PLA']

  function Editor({ seen, start, refusal = null, shop = SHOP }: { seen: ListingDraft[]; start: Partial<ListingDraft>; refusal?: ComponentProps<typeof ListingEditor>['refusal']; shop?: string[] }) {
    const [draft, setDraft] = useState<ListingDraft>({ ...emptyDraft('name-keychain', COMMIT), title: 'Tag', ...start })
    return (
      <ListingEditor
        draft={draft}
        onChange={(next) => {
          seen.push(next)
          setDraft(next)
        }}
        models={[]}
        versions={[]}
        schema={keychainSchema}
        filaments={ALL}
        media={[]}
        shopMaterials={shop}
        refusal={refusal}
      />
    )
  }

  const allowed = () => screen.getByRole('group', { name: 'Materials allowed' })
  const order = () => within(allowed()).getAllByTestId('material-row').map((row) => row.getAttribute('data-material'))

  it('shows the shop default for a listing that names no materials, and overrides it', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Editor seen={seen} start={{ colours: [RED.id] }} />)
    expect(allowed()).toHaveTextContent('Shop default (PETG, PLA)')
    expect(within(allowed()).getByRole('link', { name: 'Change the shop default' })).toHaveAttribute('href', '/settings/printing#storefront-pricing')
    await user.click(within(allowed()).getByRole('button', { name: 'Choose for this listing' }))
    expect(last(seen).materials).toEqual(['PETG', 'PLA'])
    expect(order()).toEqual(['PETG', 'PLA'])
    await user.click(within(allowed()).getByRole('button', { name: 'Use the shop default' }))
    expect(last(seen).materials).toEqual([])
    expect(allowed()).toHaveTextContent('Shop default (PETG, PLA)')
  })

  it('reorders by its up and down buttons, by keyboard too', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Editor seen={seen} start={{ materials: ['PETG', 'PLA', 'ABS'] }} />)
    expect(within(allowed()).getByRole('button', { name: 'Move PETG up' })).toBeDisabled()
    expect(within(allowed()).getByRole('button', { name: 'Move ABS down' })).toBeDisabled()
    await user.click(within(allowed()).getByRole('button', { name: 'Move PLA up' }))
    expect(last(seen).materials).toEqual(['PLA', 'PETG', 'ABS'])
    // At the top, the focus moves to the row's other button, so the keyboard can go on.
    expect(within(allowed()).getByRole('button', { name: 'Move PLA down' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(last(seen).materials).toEqual(['PETG', 'PLA', 'ABS'])
    expect(within(allowed()).getByRole('button', { name: 'Move PLA down' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(order()).toEqual(['PETG', 'ABS', 'PLA'])
  })

  it('adds a material of the price book and removes one, never the last', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Editor seen={seen} start={{ materials: ['PETG'] }} />)
    expect(within(allowed()).getByRole('button', { name: 'Remove PETG' })).toBeDisabled()
    await user.click(within(allowed()).getByRole('combobox', { name: 'Add a material' }))
    // The price book's materials not on the list yet.
    expect((await screen.findAllByRole('option')).map((option) => option.textContent)).toEqual(['ABS', 'PLA'])
    await user.click(screen.getByRole('option', { name: 'ABS' }))
    expect(last(seen).materials).toEqual(['PETG', 'ABS'])
    await user.click(within(allowed()).getByRole('button', { name: 'Remove PETG' }))
    expect(last(seen).materials).toEqual(['ABS'])
  })

  it('groups the colours on offer by allowed material, in the list’s order, with Offer all and Offer none', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Editor seen={seen} start={{ materials: ['PLA', 'PETG'], colours: [RED.id, PETG_BLACK.id] }} />)
    const colours = screen.getByRole('group', { name: 'Colours on offer' })
    await user.click(within(colours).getByRole('button', { name: /^Show all/ }))
    const groups = () => within(colours).getAllByTestId('material-colours')
    expect(groups().map((group) => group.getAttribute('aria-label'))).toEqual(['PLA colours', 'PETG colours'])
    expect(groups()[0]).toHaveTextContent('1 of 2 offered')
    // ABS is not allowed, so its filament is not listed.
    expect(within(colours).queryByRole('checkbox', { name: 'ABS Grey' })).toBeNull()

    await user.click(within(groups()[1]!).getByRole('button', { name: 'Offer all' }))
    expect(new Set(last(seen).colours)).toEqual(new Set([RED.id, PETG_BLACK.id, PETG_CLEAR.id]))
    await user.click(within(groups()[0]!).getByRole('button', { name: 'Offer none' }))
    expect(new Set(last(seen).colours)).toEqual(new Set([PETG_BLACK.id, PETG_CLEAR.id]))
    await user.click(within(groups()[0]!).getByRole('checkbox', { name: 'PLA Ocean' }))
    expect(last(seen).colours).toContain(BLUE.id)

    // Reordering the materials reorders the groups.
    await user.click(within(allowed()).getByRole('button', { name: 'Move PETG up' }))
    expect(groups().map((group) => group.getAttribute('aria-label'))).toEqual(['PETG colours', 'PLA colours'])
  })

  it('names a ticked colour whose material is not allowed, and allows it', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Editor seen={seen} start={{ colours: [RED.id, ABS_GREY.id] }} />)
    const outside = screen.getByTestId('materials-not-allowed')
    expect(outside).toHaveTextContent('ABS Grey')
    await user.click(within(outside).getByRole('button', { name: 'Allow ABS' }))
    // The shop default, with ABS last.
    expect(last(seen).materials).toEqual(['PETG', 'PLA', 'ABS'])
    expect(screen.queryByTestId('materials-not-allowed')).toBeNull()
  })

  it('unticks the colours whose material is not allowed', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Editor seen={seen} start={{ colours: [RED.id, ABS_GREY.id] }} />)
    await user.click(within(screen.getByTestId('materials-not-allowed')).getByRole('button', { name: 'Untick it' }))
    expect(last(seen).colours).toEqual([RED.id])
  })

  it('says beside the materials which filament publish refused for its material', () => {
    renderPage(<Editor seen={[]} start={{ colours: [ABS_GREY.id] }} refusal={{ kind: 'material_not_allowed', filament: ABS_GREY.id, detail: 'x' }} />)
    expect(within(allowed()).getByRole('alert')).toHaveTextContent('Not published: ABS Grey is ABS, which this listing does not allow. Allow ABS, or untick ABS Grey under Colours on offer.')
  })

  it('says beside a setting what publish refused about it (#6144, #6152)', () => {
    const { rerender } = renderPage(<Editor seen={[]} start={{ colours: [RED.id] }} refusal={{ kind: 'param', code: 'default_invalid', name: 'name', detail: 'x' }} />)
    expect(within(screen.getByRole('group', { name: 'Name on the tag' })).getByRole('alert')).toHaveTextContent(
      'Not published: “Name on the tag” starts at a value these choices do not allow. Set “Starts at” to one they allow.',
    )
    rerender(<Editor seen={[]} start={{ colours: [RED.id] }} refusal={{ kind: 'param', code: 'colour_unknown', name: 'text_color', detail: 'x' }} />)
    expect(within(screen.getByRole('group', { name: 'Text' })).getByRole('alert')).toHaveTextContent(
      'Not published: “Text” uses a colour that no colour on offer has. Pick a colour on offer, or tick a filament of that colour under “Colours on offer”.',
    )
    rerender(<Editor seen={[]} start={{ colours: [RED.id] }} refusal={{ kind: 'param', code: 'support_offered', name: 'name', detail: 'x' }} />)
    expect(within(screen.getByRole('group', { name: 'Name on the tag' })).getByRole('alert')).toHaveTextContent(
      'Not published: “Name on the tag” sets support, which stays yours and a customer never sees. Turn off “Customer can change”.',
    )
  })

  it('says beside the colours why publish found no material to print in', () => {
    renderPage(<Editor seen={[]} start={{ colours: [RED.id] }} refusal={{ kind: 'locked_colours_unprintable', names: ['text_color'], detail: 'x' }} />)
    const colours = screen.getByRole('group', { name: 'Colours on offer' })
    expect(within(colours).getByRole('alert')).toHaveTextContent(
      'Not published: no allowed material has the locked colour of Text and a colour for each setting the customer picks. Tick those colours in one material, or let the customer change Text.',
    )
  })

  it('never sends one_material, and sends the materials in their order', () => {
    const body = listingInput({ ...emptyDraft('name-keychain', COMMIT), title: 'Tag', materials: ['PLA', 'PETG'] }, keychainSchema, ALL)
    expect(body).not.toHaveProperty('one_material')
    expect(body.materials).toEqual(['PLA', 'PETG'])
  })
})

describe('the editor’s preview (#6258)', () => {
  it('renders a listing stored with no materials, with the colour cascade', () => {
    // A listing stored before #6258 (as the mocks' seeded ones) has no `materials`.
    const stored = {
      id: 'old',
      slug: 'name-keychain',
      commit: COMMIT,
      title: 'Old tag',
      description: '',
      params: {},
      fixed: {},
      colours: [RED.id, BLUE.id, CORAL.id],
      media_ids: [],
    } as unknown as StorefrontListing
    const draft = { ...draftOf(stored), ...defaultsFor(keychainSchema, [RED, BLUE, CORAL]) }
    const listing = previewListing(draft, keychainSchema, [RED, BLUE, CORAL], [], () => '')
    renderPage(<ListingView listing={listing} values={{}} onChange={() => undefined} headingLevel={2} />)
    expect(screen.getByRole('heading', { level: 2, name: 'Old tag' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Plate/ })).toHaveAccessibleName('Plate Ocean')
    expect(draftOf(stored).materials).toEqual([])
  })

  it('renders a draft with no materials, and a listing the preview gets with none', () => {
    const { materials: _none, ...draft } = { ...emptyDraft('name-keychain', COMMIT), title: 'Old tag', ...defaultsFor(keychainSchema, [RED, BLUE, CORAL]) }
    const listing = previewListing(draft as ListingDraft, keychainSchema, [RED, BLUE, CORAL], [], () => '')
    expect(listing.materials).toEqual([])
    const { materials: _drop, ...bare } = listing
    renderPage(<ListingView listing={bare as typeof listing} values={{}} onChange={() => undefined} headingLevel={2} />)
    expect(screen.getByRole('button', { name: /^Plate/ })).toHaveAccessibleName('Plate Ocean')
  })
})
