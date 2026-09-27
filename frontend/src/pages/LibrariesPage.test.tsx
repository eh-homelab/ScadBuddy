import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MISSING_REF } from '../mocks/fixtures'
import { renderPage } from '../test/utils'
import { LibrariesPage } from './LibrariesPage'

function row(name: string) {
  return screen.getByRole('listitem', { name })
}

describe('LibrariesPage', () => {
  it('lists the catalogue with what each library is pinned to', async () => {
    renderPage(<LibrariesPage />)

    await screen.findByRole('listitem', { name: 'BOSL2' })
    expect(row('BOSL2')).toHaveTextContent('v2.0.761')
    expect(row('BOSL2')).toHaveTextContent('f47030c')
    expect(row('BOSL2')).toHaveTextContent('BSD-2-Clause')
    expect(row('dotSCAD')).toHaveTextContent('Not added')
  })

  it('adds a catalogue library at its default ref', async () => {
    const { user } = renderPage(<LibrariesPage />)
    await screen.findByRole('listitem', { name: 'dotSCAD' })

    await user.click(within(row('dotSCAD')).getByRole('button', { name: 'Add' }))

    expect(await within(row('dotSCAD')).findByText(/Pinned/)).toBeInTheDocument()
    expect(row('dotSCAD')).toHaveTextContent('v3.3')
  })

  it('pins a library to another ref', async () => {
    const { user } = renderPage(<LibrariesPage />)
    await screen.findByRole('listitem', { name: 'BOSL2' })

    const ref = within(row('BOSL2')).getByLabelText('Ref')
    await user.clear(ref)
    await user.type(ref, 'v2.0.760')
    await user.click(within(row('BOSL2')).getByRole('button', { name: 'Update' }))

    expect(await within(row('BOSL2')).findByText(/v2\.0\.760/)).toBeInTheDocument()
  })

  it('says why a pin failed', async () => {
    const { user } = renderPage(<LibrariesPage />)
    await screen.findByRole('listitem', { name: 'BOSL2' })

    const ref = within(row('BOSL2')).getByLabelText('Ref')
    await user.clear(ref)
    await user.type(ref, MISSING_REF)
    await user.click(within(row('BOSL2')).getByRole('button', { name: 'Update' }))

    expect(await within(row('BOSL2')).findByRole('alert')).toHaveTextContent('git clone failed')
  })

  it('adds a library that is not in the catalogue by its URL', async () => {
    const { user } = renderPage(<LibrariesPage />)
    await screen.findByRole('listitem', { name: 'BOSL2' })

    const form = screen.getByRole('form', { name: 'Add by URL' })
    await user.type(within(form).getByLabelText('Name'), 'threads')
    await user.type(within(form).getByLabelText('Git URL'), 'https://example.com/threads.git')
    await user.type(within(form).getByLabelText('Ref'), 'v1.0')
    await user.click(within(form).getByRole('button', { name: 'Add' }))

    expect(await screen.findByRole('listitem', { name: 'threads' })).toHaveTextContent('v1.0')
  })
})
