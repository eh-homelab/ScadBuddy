import { fireEvent, screen, within } from '@testing-library/react'
import { useState, type ComponentProps } from 'react'
import { describe, expect, it } from 'vitest'
import type { CustomizerSchema, Filament, Param } from '../../api/types'
import { keychainSchema } from '../../mocks/fixtures'
import { renderPage } from '../../test/utils'
import { ListingEditor } from './ListingEditor'
import { defaultsFor, emptyDraft, listingInput, previewListing, type ListingDraft } from './listingDraft'

const COMMIT = 'a'.repeat(40)

function filament(id: string, colour: string, colour_name: string): Filament {
  return {
    id,
    material: 'PLA',
    subtype: null,
    brand: null,
    colour_name,
    colour,
    cost_per_kg: '20',
    density_g_cm3: '1.24',
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

  it('keeps an offered colour’s own label when it is saved (#6137)', () => {
    const draft: ListingDraft = {
      ...emptyDraft('name-keychain', COMMIT),
      title: 'Tag',
      colours: [RED.id],
      params: { body_color: { kind: 'choice', choices: [], label: 'Colour of the letters' } },
    }
    expect(listingInput(draft, keychainSchema, [RED]).params).toEqual({
      body_color: { kind: 'choice', choices: ['#FF0000'], label: 'Colour of the letters' },
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
