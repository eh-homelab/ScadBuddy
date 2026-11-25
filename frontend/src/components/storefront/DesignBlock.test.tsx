import { act, screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Filament } from '../../api/types'
import { DOLLHOUSE_SLUG, DOLLHOUSE_VERSION, dollhouseSchema, pipelines } from '../../mocks/fixtures'
import { server } from '../../mocks/server'
import { setUiModuleLoader } from '../../template-ui/loadModule'
import type { Host, Mount } from '../../template-ui/types'
import { renderPage } from '../../test/utils'
import { DesignBlock } from './DesignBlock'
import { emptyDraft, listingInput, type DesignSchema, type ListingDraft, type ListingRefusal } from './listingDraft'

// WebGL does not exist in jsdom.
vi.mock('../Preview', () => ({ Preview: ({ job }: { job?: { id: string } }) => <div data-testid="preview">{job?.id ?? 'no render'}</div> }))

const DECLARED = pipelines[DOLLHOUSE_SLUG]!
const SCHEMA: DesignSchema = {
  ...dollhouseSchema,
  ui: { module: 'ui/index.js', slot: 'page', api: 1 },
  ui_error: null,
  pipeline: DECLARED.declared,
  pipeline_params: DECLARED.sets,
}
const FORM: DesignSchema = { ...SCHEMA, ui: null }
const HOUSE = { cols: 3, rows: 1, storeys: 2, windows: 4 }

/** The host each mount of the template's UI got. */
let hosts: Host[] = []
let describing: (() => string) | null = null
const mount: Mount = (root, host) => {
  hosts.push(host)
  if (describing) host.describe(describing)
  root.append(document.createElement('sb-preview'))
}

beforeEach(() => {
  hosts = []
  describing = null
  setUiModuleLoader(async () => ({ mount }))
})
afterEach(() => {
  setUiModuleLoader(null)
  server.events.removeAllListeners('request:start')
})

function Harness({ start = {}, schema = SCHEMA, refusal = null, seen = [] }: { start?: Partial<ListingDraft>; schema?: DesignSchema; refusal?: ListingRefusal | null; seen?: ListingDraft[] }) {
  const [draft, setDraft] = useState<ListingDraft>({ ...emptyDraft(DOLLHOUSE_SLUG, DOLLHOUSE_VERSION), title: 'House', ...start })
  return (
    <>
      <button type="button" onClick={() => setDraft({ ...draft, commit: 'f'.repeat(40) })}>
        Another version
      </button>
      <button type="button" onClick={() => setDraft({ ...draft, inputs: structuredClone(draft.inputs) })}>
        Saved
      </button>
      <DesignBlock
        draft={draft}
        onChange={(next) => {
          seen.push(next)
          setDraft(next)
        }}
        schema={schema}
        name="Dollhouse Kit"
        filaments={[] as Filament[]}
        settled
        refusal={refusal}
      />
    </>
  )
}

const SET = { inputs: { house: HOUSE }, inputs_v: 1 }
const block = () => screen.getByRole('group', { name: 'Design' })

async function open(user: ReturnType<typeof renderPage>['user']) {
  await user.click(screen.getByRole('button', { name: 'Set up the design' }))
  const dialog = await screen.findByRole('dialog', { name: 'Set up the design' })
  await waitFor(() => expect(hosts).toHaveLength(1))
  return { dialog, host: hosts[0]! }
}

describe('the Design block (#5701 §5.1)', () => {
  it('says the design in the schema’s words, and that customers cannot change it', () => {
    renderPage(<Harness start={SET} />)
    expect(block()).toHaveTextContent('House: Modules wide 3, Modules deep 1, Storeys 2, Windows per storey 4')
    expect(block()).toHaveTextContent('Set by you. Customers cannot change it.')
    expect(block()).not.toHaveTextContent('cols')
  })

  it('says when the design is not set up, or is the template’s own start', () => {
    const { unmount } = renderPage(<Harness />)
    expect(block()).toHaveTextContent('Not set up yet')
    unmount()
    renderPage(<Harness start={{ inputs: {}, inputs_v: 0 }} />)
    expect(block()).toHaveTextContent('The design the template starts with')
  })

  it('is not there for a template with no pipeline', () => {
    renderPage(<Harness schema={{ ...SCHEMA, pipeline: null, pipeline_params: [] }} />)
    expect(screen.queryByRole('group', { name: 'Design' })).toBeNull()
  })

  it('clears a design that this version does not take', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness start={SET} schema={{ ...SCHEMA, pipeline: null, pipeline_params: [] }} seen={seen} />)
    await user.click(screen.getByRole('button', { name: 'Clear the design' }))
    expect(listingInput(seen.at(-1)!, SCHEMA, [])).not.toHaveProperty('inputs')
    expect(screen.queryByRole('group', { name: 'Design' })).toBeNull()
  })

  it('says why a version cannot be listed, and offers no setup of it', () => {
    renderPage(<Harness schema={{ ...SCHEMA, pipeline: { inputs_version: 1, inputs: null, fault: 'it declares no shape' } }} />)
    expect(block()).toHaveTextContent('cannot be listed: it declares no shape.')
    expect(screen.queryByRole('button', { name: 'Set up the design' })).toBeNull()
  })

  it('shows a refusal with the schema’s title, never the path', () => {
    renderPage(<Harness start={SET} refusal={{ kind: 'inputs', code: 'invalid', name: 'inputs.house.storeys', reason: 'maximum', detail: 'inputs.house.storeys: 9 is greater' }} />)
    expect(within(block()).getByRole('alert')).toHaveTextContent('Storeys: at most 3.')
    expect(block()).not.toHaveTextContent('inputs.')
  })

  it('takes an input this version does not have off the listing', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(
      <Harness
        start={{ inputs: { house: { ...HOUSE, attic: 1 } }, inputs_v: 1 }}
        refusal={{ kind: 'inputs', code: 'invalid', name: 'inputs.house.attic', reason: 'additional', detail: 'inputs.house.attic: unknown' }}
        seen={seen}
      />,
    )
    await user.click(screen.getByRole('button', { name: 'Remove it' }))
    expect(seen.at(-1)?.inputs).toEqual({ house: HOUSE })
  })

  it('shows a refusal of a parameter the design sets, which has no row', () => {
    renderPage(<Harness start={SET} refusal={{ kind: 'param', code: 'pipeline_set', name: 'piece', detail: 'no' }} />)
    expect(within(block()).getByRole('alert')).toHaveTextContent('the design sets “Piece” itself')
  })

  it('leaves a refusal of another kind of a parameter with no row to the page (#7660 review)', () => {
    renderPage(<Harness start={SET} refusal={{ kind: 'param', code: 'default_invalid', name: 'gone', detail: 'no' }} />)
    expect(within(block()).queryByRole('alert')).toBeNull()
  })
})

describe('"Set up the design" (#5701 §5.2, §5.3)', () => {
  it('sets the listing’s inputs, their version and its settings only on "Use this design"', async () => {
    const seen: ListingDraft[] = []
    describing = () => '4×1-module house'
    const { user } = renderPage(<Harness start={SET} seen={seen} />)
    const { dialog, host } = await open(user)
    expect(host.inputs.get()).toMatchObject({ house: HOUSE, v: 1 })
    act(() => host.inputs.set({ house: { cols: 4 }, params: { course_height: 240 } }))
    expect(host.inputs.get()).toMatchObject({ house: { ...HOUSE, cols: 4 }, params: { course_height: 240 } })
    expect(seen).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Use this design' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(seen.at(-1)).toMatchObject({ inputs: { house: { ...HOUSE, cols: 4 } }, inputs_v: 1, fixed: { course_height: 240 } })
    expect(seen.at(-1)?.inputs).not.toHaveProperty('params')
    // The template's own words, while these are the inputs.
    expect(block()).toHaveTextContent('4×1-module house')
  })

  it('keeps the template’s words when a save gives the same design back (#7660 review)', async () => {
    describing = () => '4×1-module house'
    const { user } = renderPage(<Harness start={SET} />)
    const { dialog } = await open(user)
    await user.click(within(dialog).getByRole('button', { name: 'Use this design' }))
    expect(block()).toHaveTextContent('4×1-module house')
    await user.click(screen.getByRole('button', { name: 'Saved' }))
    expect(block()).toHaveTextContent('4×1-module house')
  })

  it('keeps the schema’s words when the template cannot describe the design', async () => {
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    describing = () => {
      throw new Error('broken')
    }
    const { user } = renderPage(<Harness start={SET} />)
    const { dialog } = await open(user)
    await user.click(within(dialog).getByRole('button', { name: 'Use this design' }))
    expect(block()).toHaveTextContent('House: Modules wide 3')
    quiet.mockRestore()
  })

  it('fills a listing with no design in from the template’s own start', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness seen={seen} />)
    const { dialog, host } = await open(user)
    expect(host.inputs.get()).toMatchObject({ house: DECLARED.fresh['house'], v: 1 })
    await user.click(within(dialog).getByRole('button', { name: 'Use this design' }))
    expect(seen.at(-1)).toMatchObject({ inputs: DECLARED.fresh, inputs_v: 1 })
  })

  it('leaves the listing alone on Cancel, and gives the focus back', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness start={SET} seen={seen} />)
    const { dialog, host } = await open(user)
    act(() => host.inputs.set({ house: { cols: 1 } }))
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(seen).toEqual([])
    expect(screen.getByRole('button', { name: 'Set up the design' })).toHaveFocus()
  })

  it('asks before Escape drops changes, and closes at once with none', async () => {
    const { user } = renderPage(<Harness start={SET} />)
    const { host } = await open(user)
    act(() => host.inputs.set({ house: { cols: 1 } }))
    await user.keyboard('{Escape}')
    const ask = await screen.findByRole('alertdialog')
    await user.click(within(ask).getByRole('button', { name: 'Keep editing' }))
    expect(host.inputs.get()).toMatchObject({ house: { cols: 1 } })
    // Back as it was, there is nothing to lose.
    act(() => host.inputs.set({ house: { cols: 3 } }))
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('closes when the listing moves to another version, and the old one’s writes reach nothing', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness start={SET} seen={seen} />)
    const { host } = await open(user)
    act(() => screen.getByText('Another version').click())
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const quiet = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    host.inputs.set({ house: { cols: 1 } })
    quiet.mockRestore()
    expect(seen).toEqual([])
  })

  it('renders the working copy at the listing’s version and saves no output, preset or print', async () => {
    const bodies: { version?: string | null; inputs?: unknown }[] = []
    const others: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method !== 'GET' && !/\/(render|migrate)$/.test(new URL(request.url).pathname)) others.push(request.url)
      if (request.url.endsWith('/render')) void request.clone().json().then((body: (typeof bodies)[number]) => bodies.push(body))
    })
    const { user } = renderPage(<Harness start={SET} />)
    const { host } = await open(user)
    act(() => host.inputs.set({ house: { cols: 4 } }))
    expect(await host.generate()).toMatchObject({ outputId: '' })
    expect(bodies.at(-1)).toMatchObject({ version: DOLLHOUSE_VERSION, inputs: { house: { ...HOUSE, cols: 4 } } })
    host.openPrint('x')
    await expect(host.presets.save('Mine')).rejects.toThrow('a preset is saved on the template’s own page')
    expect(others).toEqual([])
  })

  it('offers to try again when the design could not be opened', async () => {
    let answers = 0
    server.use(
      http.post('/api/v1/models/:slug/inputs/migrate', async ({ request }) => {
        answers += 1
        if (answers === 1) return HttpResponse.json({ title: 'Service', status: 500, detail: 'no worker' }, { status: 500 })
        const { inputs } = (await request.json()) as { inputs: Record<string, unknown> }
        return HttpResponse.json({ inputs: { ...inputs, house: HOUSE, v: 1 }, from_version: 0, to_version: 1 })
      }),
    )
    const { user } = renderPage(<Harness start={{ inputs: {}, inputs_v: 0 }} />)
    await user.click(screen.getByRole('button', { name: 'Set up the design' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The design could not be opened')
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(hosts).toHaveLength(1))
    expect(hosts[0]!.inputs.get()).toMatchObject({ house: HOUSE, v: 1 })
  })
})

describe('the form drawn from the inputs’ schema (#5701 §5.2)', () => {
  it('sets the design for a template with no interface', async () => {
    const seen: ListingDraft[] = []
    const { user } = renderPage(<Harness start={SET} schema={FORM} seen={seen} />)
    await user.click(screen.getByRole('button', { name: 'Set up the design' }))
    const house = await screen.findByRole('group', { name: 'House' })
    const storeys = within(house).getByLabelText(/^Storeys/)
    expect(storeys).toHaveValue(2)
    await user.clear(storeys)
    await user.click(screen.getByRole('button', { name: 'Use this design' }))
    expect(within(house).getByText('Required')).toBeInTheDocument()
    expect(storeys).toHaveFocus()
    expect(seen).toEqual([])
    await user.type(storeys, '3')
    await user.click(screen.getByRole('button', { name: 'Use this design' }))
    expect(seen.at(-1)).toMatchObject({ inputs: { house: { ...HOUSE, storeys: 3 } }, inputs_v: 1 })
    expect(block()).toHaveTextContent('Storeys 3')
  })

  it('takes over, with the work so far, when the template’s interface does not mount', async () => {
    setUiModuleLoader(async () => ({
      mount: () => {
        throw new Error('no canvas')
      },
    }))
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const { user } = renderPage(<Harness start={SET} />)
    await user.click(screen.getByRole('button', { name: 'Set up the design' }))
    expect(await screen.findByRole('group', { name: 'House' })).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent('ui/index.js: no canvas')
    quiet.mockRestore()
  })
})
