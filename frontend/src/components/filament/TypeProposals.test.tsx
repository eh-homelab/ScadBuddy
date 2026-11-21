import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { TypeProposal, TypeProposalPick } from '../../api/filament'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { TypeProposals } from './TypeProposals'

const PROPOSALS = '/api/v1/filament/type-proposals'

/** Every confirm body, before the mock answers it. */
function watchConfirms() {
  const sent: TypeProposalPick[][] = []
  server.use(
    http.post(`${PROPOSALS}/confirm`, async ({ request }) => {
      sent.push(((await request.clone().json()) as { groups: TypeProposalPick[] }).groups)
    }),
  )
  return sent
}

function open(onDone = vi.fn()) {
  return { onDone, ...renderPage(<TypeProposals onDone={onDone} />) }
}

const group = (over: Partial<TypeProposal>): TypeProposal => ({
  code: 'PLA-RED',
  type_id: null,
  brand_id: null,
  material_id: '00000000-0000-4000-8000-0000000000a1',
  subtype_id: null,
  rgba: '#FF0000FF',
  label_weight_g: 1000,
  material_number: null,
  brand: null,
  material: 'PLA',
  subtype: null,
  colour_name: 'Red',
  spool_ids: ['00000000-0000-4000-8000-0000000000b1'],
  numbers: [31],
  count: 1,
  ...over,
})

describe('the Create types panel (#6701)', () => {
  it('lists a row per proposed type and the spools it cannot group', async () => {
    open()
    const row = await screen.findByRole('listitem', { name: 'PLA-TEAL' })
    expect(within(row).getByRole('textbox', { name: 'Type name' })).toHaveValue('PLA-TEAL')
    expect(within(row).getByText('PLA, Teal, 1000 g')).toBeInTheDocument()
    expect(within(row).getByText('1 spool: #6')).toBeInTheDocument()
    expect(within(row).getByTestId('rgba-swatch')).toHaveAttribute('data-colour', '#1B9AAAFF')
    const ungrouped = screen.getByRole('list', { name: 'Not enough to propose a type' })
    expect(within(ungrouped).getByRole('listitem')).toHaveTextContent('#7 needs a label weight')
  })

  it('creates one type with the edited name, then says what it did', async () => {
    const sent = watchConfirms()
    const { user, onDone } = open()
    const row = await screen.findByRole('listitem', { name: 'PLA-TEAL' })
    const name = within(row).getByRole('textbox', { name: 'Type name' })
    await user.clear(name)
    await user.type(name, 'Teal PLA')
    await user.click(within(row).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(onDone).toHaveBeenCalledWith('Created Teal PLA for 1 spool.'))
    expect(sent).toHaveLength(1)
    expect(sent[0]).toEqual([
      expect.objectContaining({ code: 'Teal PLA', rgba: '#1B9AAAFF', label_weight_g: 1000, spool_ids: [expect.any(String)] }),
    ])
    await waitFor(() => expect(screen.queryByRole('listitem', { name: 'PLA-TEAL' })).not.toBeInTheDocument())
  })

  it('creates every type at once and names the spools it skipped', async () => {
    const groups = [group({}), group({ code: 'PLA-BLUE', rgba: '#0000FFFF', colour_name: 'Blue', spool_ids: ['00000000-0000-4000-8000-0000000000b2'], numbers: [32] })]
    server.use(
      http.get(PROPOSALS, () => HttpResponse.json({ groups, ungrouped: [] })),
      http.post(`${PROPOSALS}/confirm`, () =>
        HttpResponse.json({
          groups: [
            { code: 'PLA-RED', type_id: 'a', made: true, new: true, typed: groups[0]!.spool_ids, skipped: [] },
            { code: 'PLA-BLUE', type_id: null, made: false, new: false, typed: [], skipped: [{ spool_id: groups[1]!.spool_ids[0], reason: 'typed' }] },
          ],
        }),
      ),
    )
    const sent = watchConfirms()
    const { user, onDone } = open()
    await user.click(await screen.findByRole('button', { name: 'Create all 2 types' }))
    await waitFor(() => expect(onDone).toHaveBeenCalledWith('Created PLA-RED for 1 spool. Skipped #32: it has a type now.'))
    expect(sent[0]!.map((pick) => pick.code)).toEqual(['PLA-RED', 'PLA-BLUE'])
  })

  it('says when a row joined the type another row just created, and the name it did not use (R-6729-1)', async () => {
    const groups = [
      group({ code: 'GFA00', material_number: 'GFA00' }),
      group({ code: 'GFA01', material_number: 'GFA01', spool_ids: ['00000000-0000-4000-8000-0000000000b2'], numbers: [32] }),
    ]
    server.use(
      http.get(PROPOSALS, () => HttpResponse.json({ groups, ungrouped: [] })),
      http.post(`${PROPOSALS}/confirm`, () =>
        HttpResponse.json({
          groups: [
            { code: 'GFA00', type_id: 'a', made: true, new: true, typed: groups[0]!.spool_ids, skipped: [] },
            { code: 'GFA00', type_id: 'a', made: false, new: true, typed: groups[1]!.spool_ids, skipped: [] },
          ],
        }),
      ),
    )
    const { user, onDone } = open()
    await user.click(await screen.findByRole('button', { name: 'Create all 2 types' }))
    await waitFor(() =>
      expect(onDone).toHaveBeenCalledWith(
        'Created GFA00 for 1 spool. Typed 1 spool as GFA00, which another row created; the name GFA01 was not used.',
      ),
    )
  })

  it('says why a create was refused', async () => {
    server.use(
      http.post(`${PROPOSALS}/confirm`, () =>
        HttpResponse.json({ detail: 'a spool type already has one of these codes; choose another name' }, { status: 409 }),
      ),
    )
    const { user, onDone } = open()
    const row = await screen.findByRole('listitem', { name: 'PLA-TEAL' })
    await user.click(within(row).getByRole('button', { name: 'Create' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('choose another name')
    expect(onDone).not.toHaveBeenCalled()
  })

  it('says so when the proposals could not be loaded', async () => {
    server.use(http.get(PROPOSALS, () => HttpResponse.json({ detail: 'the database is down' }, { status: 503 })))
    open()
    expect(await screen.findByRole('alert')).toHaveTextContent('the database is down')
  })

  it('keys each row by its spools when two rows use one type', async () => {
    const kind = '00000000-0000-4000-8000-0000000000c1'
    const groups = [
      group({ type_id: kind }),
      group({ type_id: kind, label_weight_g: 250, spool_ids: ['00000000-0000-4000-8000-0000000000b2'], numbers: [32] }),
    ]
    server.use(http.get(PROPOSALS, () => HttpResponse.json({ groups, ungrouped: [] })))
    const errors = vi.spyOn(console, 'error')
    open()
    const rows = await screen.findAllByRole('listitem', { name: 'PLA-RED' })
    expect(rows.map((row) => within(row).getByText(/^1 spool/).textContent)).toEqual([
      '1 spool: #31 (type PLA-RED exists and is used)',
      '1 spool: #32 (type PLA-RED exists and is used)',
    ])
    expect(errors.mock.calls.flat().join(' ')).not.toContain('same key')
    errors.mockRestore()
  })

  it('shows nothing when no spool needs a type', async () => {
    server.use(http.get(PROPOSALS, () => HttpResponse.json({ groups: [], ungrouped: [] })))
    const { container } = open()
    await waitFor(() => expect(container).toBeEmptyDOMElement())
  })
})
