import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { addAssociation } from '../../mocks/features/associations'
import { resetMockState } from '../../mocks/handlers'
import type { AssociationSubject } from '../../lib/associations'
import { RelatedAssociations } from './RelatedAssociations'

// #6395 — a library file's or a model's associations: each said from this side, with a
// link to the other end; a stored one removable, an implied one saying why it is there.

const FILE = 1_000_000_498
const OTHER = 1_000_000_499

beforeEach(() => resetMockState())

function renderRelated(subject: AssociationSubject) {
  const user = userEvent.setup()
  render(
    <MemoryRouter>
      <RelatedAssociations subject={subject} />
    </MemoryRouter>,
  )
  return user
}

describe('RelatedAssociations', () => {
  it("says each association from the file's side and from the target's", async () => {
    addAssociation({
      library_file_id: FILE,
      library_file_name: 'fixed.3mf',
      target: 'model:unicorn-pencil-holder',
      target_name: 'Unicorn pencil holder',
      relation: 'output_of',
      note: 'scaled to 110%',
    })
    addAssociation({
      library_file_id: OTHER,
      library_file_name: 'petg-supports.3mf',
      target: `library:${FILE}`,
      target_name: 'fixed.3mf',
      relation: 'variant_of',
    })
    renderRelated({ kind: 'library', id: FILE })
    const list = await screen.findByRole('list')
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    const [own, theirs] = items as [HTMLElement, HTMLElement]
    expect(own).toHaveTextContent('Output of Unicorn pencil holder (model)')
    expect(own).toHaveTextContent('scaled to 110%')
    expect(within(own).getByRole('link')).toHaveAttribute('href', '/m/unicorn-pencil-holder')
    expect(theirs).toHaveTextContent('Has variant petg-supports.3mf')
    expect(within(theirs).getByRole('link')).toHaveAttribute('href', `/library?details=${OTHER}`)
  })

  it('lists an implied association without a remove button, saying why', async () => {
    addAssociation({
      id: null,
      library_file_id: FILE,
      library_file_name: 'fixed.3mf',
      target: 'model:unicorn-pencil-holder',
      target_name: 'Unicorn pencil holder',
      relation: 'source_of',
      implied_by: 'mesh_source',
      created_at: null,
      created_by: null,
    })
    renderRelated({ kind: 'model', slug: 'unicorn-pencil-holder' })
    const item = await screen.findByRole('listitem')
    expect(item).toHaveTextContent('Made from fixed.3mf')
    expect(item).toHaveTextContent('This model was made from the file')
    expect(within(item).queryByRole('button')).not.toBeInTheDocument()
  })

  it('removes a stored association', async () => {
    addAssociation({ library_file_id: FILE, target: 'model:name-keychain', target_name: 'Name Keychain', relation: 'related_to' })
    const user = renderRelated({ kind: 'library', id: FILE })
    await user.click(await screen.findByRole('button', { name: 'Remove related to Name Keychain' }))
    expect(await screen.findByText(/Nothing is associated yet/)).toBeInTheDocument()
  })

  it('associates the file with a model picked by name, with a relation and a note', async () => {
    const user = renderRelated({ kind: 'library', id: FILE })
    await screen.findByText(/Nothing is associated yet/)
    await user.click(screen.getByRole('button', { name: /Associate/ }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Model'), 'keychain')
    await user.click(await within(dialog).findByRole('radio', { name: 'Name Keychain' }))
    await user.click(within(dialog).getByRole('radio', { name: 'Output of' }))
    expect(within(dialog).getByTestId('associate-sentence')).toHaveTextContent('This file is output of Name Keychain.')
    await user.type(within(dialog).getByLabelText('Note (optional)'), 'fixed and scaled')
    await user.click(within(dialog).getByRole('button', { name: 'Associate' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    const item = await screen.findByRole('listitem')
    expect(item).toHaveTextContent('Output of')
    expect(item).toHaveTextContent('fixed and scaled')
  })

  it("on a model's page picks a library file, which is the file side", async () => {
    const user = renderRelated({ kind: 'model', slug: 'name-keychain' })
    await screen.findByText(/Nothing is associated yet/)
    await user.click(screen.getByRole('button', { name: /Associate/ }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByRole('group', { name: 'Associate with' })).not.toBeInTheDocument()
    await user.type(within(dialog).getByLabelText('Library file'), 'drawer')
    await user.click(await within(dialog).findByRole('radio', { name: 'drawer-label.3mf' }))
    expect(within(dialog).getByTestId('associate-sentence')).toHaveTextContent(
      'drawer-label.3mf is related to this model.',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Associate' }))
    const item = await screen.findByRole('listitem')
    expect(item).toHaveTextContent('Related to drawer-label.3mf')
  })
})
