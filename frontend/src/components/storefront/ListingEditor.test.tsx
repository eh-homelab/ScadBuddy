import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import type { CustomizerSchema, Filament, Param } from '../../api/types'
import { keychainSchema } from '../../mocks/fixtures'
import { ListingEditor } from './ListingEditor'
import { defaultsFor, emptyDraft, listingInput, previewListing, type ListingDraft } from './listingDraft'

const COMMIT = 'a'.repeat(40)
const PLA: Filament = {
  id: 'f1',
  material: 'PLA',
  subtype: null,
  brand: null,
  colour_name: 'Red',
  colour: '#FF0000',
  cost_per_kg: '20',
  density_g_cm3: null,
  archived_at: null,
  created_at: '2026-11-01T00:00:00Z',
  updated_at: '2026-11-01T00:00:00Z',
}

function Harness({ seen, schema = keychainSchema }: { seen: ListingDraft[]; schema?: CustomizerSchema }) {
  const [draft, setDraft] = useState<ListingDraft>({ ...emptyDraft('name-keychain', COMMIT), ...defaultsFor(schema) })
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
      filaments={[PLA]}
      media={[]}
    />
  )
}

function last(seen: ListingDraft[]): ListingDraft {
  const draft = seen.at(-1)
  if (!draft) throw new Error('no change was made')
  return draft
}

describe('ListingEditor (#5282)', () => {
  it('starts a listing with the whole of each ranged parameter offered', () => {
    const { params, fixed } = defaultsFor(keychainSchema)
    expect(params['text_size']).toEqual({ kind: 'number', min: 6, max: 28, step: 0.5 })
    expect(params['hole_side']).toEqual({ kind: 'choice', choices: ['left', 'right', 'top'] })
    expect(params['keyring_hole']).toEqual({ kind: 'choice', choices: [true, false] })
    expect(params['name']).toEqual({ kind: 'text', max_length: 20 })
    // No range, a font and a colour start locked at their defaults.
    expect(fixed).toEqual({ padding: 6, corner_radius: 4, font: 'Liberation Sans:style=Bold', body_color: '#1B6CA8', text_color: '#E8532F' })
  })

  it('locks and offers a parameter, and narrows its bounds', async () => {
    const user = userEvent.setup()
    const seen: ListingDraft[] = []
    render(<Harness seen={seen} />)
    const padding = screen.getByRole('group', { name: /Margin around the text/ })
    await user.click(within(padding).getByRole('radio', { name: 'Customer may change' }))
    expect(last(seen).params['padding']).toEqual({ kind: 'number', min: 6, max: 6, step: null })
    expect(last(seen).fixed['padding']).toBeUndefined()
    const most = within(padding).getByLabelText('Most Margin around the text')
    fireEvent.change(most, { target: { value: '12' } })
    expect(last(seen).params['padding']).toMatchObject({ max: 12 })

    const side = screen.getByRole('group', { name: /Hole position/ })
    await user.click(within(side).getByRole('checkbox', { name: 'Offer Top centre' }))
    expect(last(seen).params['hole_side']).toEqual({ kind: 'choice', choices: ['left', 'right'] })

    const size = screen.getByRole('group', { name: /Text size/ })
    await user.click(within(size).getByRole('radio', { name: 'Locked at 14' }))
    expect(last(seen).params['text_size']).toBeUndefined()
    expect(last(seen).fixed['text_size']).toBe(14)
  })

  it('never offers a font', () => {
    render(<Harness seen={[]} />)
    const font = screen.getByRole('group', { name: /Typeface/ })
    expect(within(font).getByRole('radio', { name: 'Customer may change' })).toBeDisabled()
  })

  it('gives an offered colour the listing filaments’ colours when it is saved', () => {
    const draft: ListingDraft = {
      ...emptyDraft('name-keychain', COMMIT),
      title: 'Tag',
      colours: [PLA.id],
      params: { body_color: { kind: 'choice', choices: [] } },
    }
    expect(listingInput(draft, keychainSchema, [PLA]).params).toEqual({ body_color: { kind: 'choice', choices: ['#FF0000'] } })
  })

  it('keeps an offered colour’s own label and default when it is saved (#6137, #6144)', () => {
    const draft: ListingDraft = {
      ...emptyDraft('name-keychain', COMMIT),
      title: 'Tag',
      colours: [PLA.id],
      params: { body_color: { kind: 'choice', choices: [], label: 'Colour of the letters', default: '#FF0000' } },
    }
    expect(listingInput(draft, keychainSchema, [PLA]).params).toEqual({
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
      colours: [PLA.id],
      params: {
        body_color: { kind: 'choice', choices: [] },
        line: { kind: 'text', max_length: 20 },
        size: { kind: 'number', min: 20, max: 80, step: 1, label: 'How big' },
      },
    }
    const shown = previewListing(draft, schema, [PLA], [], () => '').params
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
      colours: [PLA.id],
      params: {
        body_color: { kind: 'choice', choices: [] },
        shape: { kind: 'choice', choices: ['round', 'square'] },
        size: { kind: 'number', min: 20, max: 80, step: 1 },
        line: { kind: 'text', max_length: 20, default: 'Ada' },
      },
    }
    const shown = previewListing(draft, schema, [PLA], [], () => '').params
    expect(shown.body_color!.default).toBe('#ff0000')
    expect(shown.shape!.default).toBe('round')
    expect(shown.size!.default).toBe(40)
    expect(shown.line).toMatchObject({ default: 'Ada', placeholder: 'Ada' })
    const elsewhere = previewListing({ ...draft, colours: [] }, schema, [PLA], [], () => '').params
    expect(elsewhere.body_color!.default).toBeNull()
  })

  it('locks a numeric select at one of its options, as a number', async () => {
    const user = userEvent.setup()
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
    render(<Harness seen={seen} schema={{ ...keychainSchema, parameters: [...(keychainSchema.parameters ?? []), lines] }} />)
    const group = screen.getByRole('group', { name: /Lines/ })
    await user.click(within(group).getByRole('radio', { name: 'Locked at 1' }))
    await user.click(within(group).getByRole('combobox', { name: 'Locked value of Lines' }))
    await user.click(await screen.findByRole('option', { name: 'Two' }))
    expect(last(seen).fixed['lines']).toBe(2)
  })

  it('locks a switch on or off', async () => {
    const user = userEvent.setup()
    const seen: ListingDraft[] = []
    render(<Harness seen={seen} />)
    const group = screen.getByRole('group', { name: /Keyring hole/ })
    await user.click(within(group).getByRole('radio', { name: 'Locked at on' }))
    const value = within(group).getByRole('switch', { name: 'Locked value of Keyring hole' })
    expect(value).toBeChecked()
    await user.click(value)
    expect(last(seen).fixed['keyring_hole']).toBe(false)
  })
})
