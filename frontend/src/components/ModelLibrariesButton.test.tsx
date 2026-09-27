import { screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { MISSING_REF, models } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { ModelLibrariesButton } from './ModelLibrariesButton'

const BOSL2_URL = 'https://github.com/BelfrySCAD/BOSL2.git'

function row(name: string) {
  return screen.getByRole('listitem', { name })
}

/** Records every library PUT/DELETE as `METHOD name body`. */
function watchPins() {
  const seen: string[] = []
  server.events.on('request:start', async ({ request }) => {
    const match = /\/libraries\/([^/]+)$/.exec(new URL(request.url).pathname)
    if (!match) return
    const body = request.method === 'PUT' ? ` ${JSON.stringify(await request.clone().json())}` : ''
    seen.push(`${request.method} ${decodeURIComponent(match[1] ?? '')}${body}`)
  })
  return seen
}

/** name-keychain with BOSL2 already pinned, through the real PUT route. */
async function withBosl2() {
  await api.pinModelLibrary('name-keychain', 'BOSL2', {})
}

async function openDialog(user: ReturnType<typeof renderPage>['user'], label = 'Libraries') {
  await user.click(await screen.findByRole('button', { name: new RegExp(`^${label}`) }))
  return screen.findByRole('dialog', { name: 'Libraries for Name Keychain' })
}

describe('ModelLibrariesButton', () => {
  it('adds a catalogue library at its default ref and counts it on the button', async () => {
    const seen = watchPins()
    const onSaved = vi.fn()
    const { user } = renderPage(
      <ModelLibrariesButton slug="name-keychain" name="Name Keychain" onSaved={onSaved} />,
    )

    const dialog = await openDialog(user)
    const catalogue = await within(dialog).findByRole('list', { name: 'Catalogue' })
    expect(within(catalogue).getByRole('listitem', { name: 'BOSL2' })).toHaveTextContent(
      'BSD-2-Clause',
    )
    expect(within(row('BOSL2')).getByRole('link', { name: 'BOSL2' })).toHaveAttribute(
      'href',
      'https://github.com/BelfrySCAD/BOSL2',
    )
    expect(within(row('BOSL2')).getByLabelText('Ref')).toHaveValue('v2.0.761')

    await user.click(within(row('BOSL2')).getByRole('button', { name: 'Add' }))

    const pinned = await within(dialog).findByRole('list', { name: 'Pinned libraries' })
    expect(within(pinned).getByRole('listitem', { name: 'BOSL2' })).toHaveTextContent(
      /Pinned to v2\.0\.761 at c{7}/,
    )
    // Once pinned it leaves the catalogue list; dotSCAD is still on offer.
    expect(within(within(dialog).getByRole('list', { name: 'Catalogue' })).queryByRole('listitem', {
      name: 'BOSL2',
    })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Libraries\s*1$/ })).toBeInTheDocument()
    expect(seen).toEqual([
      `PUT BOSL2 ${JSON.stringify({ url: BOSL2_URL, ref: 'v2.0.761' })}`,
    ])

    // The schema is re-read once the user is done, not under the open dialog.
    expect(onSaved).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Done' }))
    expect(onSaved).toHaveBeenCalledTimes(1)
  })

  it('adds a catalogue library at another ref', async () => {
    const seen = watchPins()
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)
    await openDialog(user)
    await screen.findByRole('listitem', { name: 'dotSCAD' })

    const ref = within(row('dotSCAD')).getByLabelText('Ref')
    await user.clear(ref)
    await user.type(ref, 'v3.2')
    await user.click(within(row('dotSCAD')).getByRole('button', { name: 'Add' }))

    expect(await within(await screen.findByRole('list', { name: 'Pinned libraries' })).findByRole(
      'listitem',
      { name: 'dotSCAD' },
    )).toHaveTextContent('Pinned to v3.2')
    expect(seen).toEqual([
      `PUT dotSCAD ${JSON.stringify({ url: 'https://github.com/JustinSDK/dotSCAD.git', ref: 'v3.2' })}`,
    ])
  })

  it('re-pins a pinned library to another ref with its own url', async () => {
    await withBosl2()
    const seen = watchPins()
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)
    await openDialog(user, 'Libraries\\s*1')
    await screen.findByRole('list', { name: 'Pinned libraries' })

    const ref = within(row('BOSL2')).getByLabelText('Ref')
    await user.clear(ref)
    await user.type(ref, 'v2.0.760')
    await user.click(within(row('BOSL2')).getByRole('button', { name: 'Update' }))

    expect(await within(row('BOSL2')).findByText(/v2\.0\.760/)).toBeInTheDocument()
    expect(seen).toEqual([`PUT BOSL2 ${JSON.stringify({ url: BOSL2_URL, ref: 'v2.0.760' })}`])
  })

  it('removes a pin and offers it from the catalogue again', async () => {
    await withBosl2()
    const seen = watchPins()
    const onSaved = vi.fn()
    const { user } = renderPage(
      <ModelLibrariesButton slug="name-keychain" name="Name Keychain" onSaved={onSaved} />,
    )
    const dialog = await openDialog(user, 'Libraries\\s*1')
    await screen.findByRole('list', { name: 'Pinned libraries' })

    await user.click(within(row('BOSL2')).getByRole('button', { name: 'Remove' }))

    expect(await within(dialog).findByText(/None yet/)).toBeInTheDocument()
    expect(
      within(screen.getByRole('list', { name: 'Catalogue' })).getByRole('listitem', {
        name: 'BOSL2',
      }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Libraries$/ })).toBeInTheDocument()
    expect(seen).toEqual(['DELETE BOSL2'])

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(onSaved).toHaveBeenCalledTimes(1)
  })

  it('says why a pin failed, on that row, and keeps the dialog open', async () => {
    await withBosl2()
    const onSaved = vi.fn()
    const { user } = renderPage(
      <ModelLibrariesButton slug="name-keychain" name="Name Keychain" onSaved={onSaved} />,
    )
    await openDialog(user, 'Libraries\\s*1')
    await screen.findByRole('list', { name: 'Pinned libraries' })

    const ref = within(row('BOSL2')).getByLabelText('Ref')
    await user.clear(ref)
    await user.type(ref, MISSING_REF)
    await user.click(within(row('BOSL2')).getByRole('button', { name: 'Update' }))

    expect(await within(row('BOSL2')).findByRole('alert')).toHaveTextContent('git clone failed')
    expect(row('BOSL2')).toHaveTextContent('Pinned to v2.0.761')
    await user.click(screen.getByRole('button', { name: 'Done' }))
    expect(onSaved).not.toHaveBeenCalled()
  })

  it('adds a library that is not in the catalogue by its URL', async () => {
    const seen = watchPins()
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)
    await openDialog(user)

    const form = await screen.findByRole('form', { name: 'Add by URL' })
    await user.type(within(form).getByLabelText('Name'), 'threads')
    await user.type(within(form).getByLabelText('Git URL'), 'https://example.com/threads.git')
    await user.type(within(form).getByLabelText('Ref'), 'v1.0')
    await user.click(within(form).getByRole('button', { name: 'Add' }))

    expect(await screen.findByRole('listitem', { name: 'threads' })).toHaveTextContent('v1.0')
    expect(within(form).getByLabelText('Name')).toHaveValue('')
    expect(seen).toEqual([
      `PUT threads ${JSON.stringify({ url: 'https://example.com/threads.git', ref: 'v1.0' })}`,
    ])
  })

  it('says why an add by URL was refused', async () => {
    server.use(
      http.put('/api/v1/models/:slug/libraries/:name', () =>
        HttpResponse.json(
          { title: 'Unprocessable Content', status: 422, detail: 'not an https git URL' },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)
    await openDialog(user)

    const form = await screen.findByRole('form', { name: 'Add by URL' })
    await user.type(within(form).getByLabelText('Name'), 'threads')
    await user.type(within(form).getByLabelText('Git URL'), 'git@example.com:threads.git')
    await user.type(within(form).getByLabelText('Ref'), 'v1.0')
    await user.click(within(form).getByRole('button', { name: 'Add' }))

    expect(await within(form).findByRole('alert')).toHaveTextContent('not an https git URL')
    expect(within(form).getByLabelText('Name')).toHaveValue('threads')
  })

  it('says so when it cannot read the model', async () => {
    server.use(
      http.get('/api/v1/models/:slug', () =>
        HttpResponse.json(
          { title: 'Internal Server Error', status: 500, detail: 'git timed out' },
          { status: 500, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)
    await openDialog(user)

    expect(await screen.findByRole('alert')).toHaveTextContent('git timed out')
    expect(screen.queryByRole('button', { name: 'Add' })).not.toBeInTheDocument()
  })

  it('keeps a new pin when the first read of the model lands after it', async () => {
    let release = () => {}
    const held = new Promise<void>((resolve) => {
      release = resolve
    })
    const stale = models.find((model) => model.slug === 'name-keychain')
    server.use(
      http.get(
        '/api/v1/models/:slug',
        async () => {
          await held
          return HttpResponse.json({ ...stale, libraries: [] })
        },
        { once: true },
      ),
    )
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)
    const dialog = await openDialog(user)
    await within(dialog).findByRole('list', { name: 'Catalogue' })
    await user.click(within(row('BOSL2')).getByRole('button', { name: 'Add' }))
    await within(dialog).findByRole('list', { name: 'Pinned libraries' })
    await user.click(within(dialog).getByRole('button', { name: 'Done' }))

    release()

    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.getByRole('button', { name: /^Libraries/ })).toHaveTextContent('Libraries1')
  })

  it("keeps the open dialog's read when the first read lands after it", async () => {
    let release = () => {}
    const held = new Promise<void>((resolve) => {
      release = resolve
    })
    const model = models.find((entry) => entry.slug === 'name-keychain')
    const stale = {
      name: 'BOSL2',
      url: BOSL2_URL,
      ref: 'v2.0.700',
      commit: 'b'.repeat(40),
    }
    server.use(
      http.get(
        '/api/v1/models/:slug',
        async () => {
          await held
          return HttpResponse.json({ ...model, libraries: [stale] })
        },
        { once: true },
      ),
    )
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)
    const dialog = await openDialog(user)
    expect(await within(dialog).findByText(/None yet/)).toBeInTheDocument()

    release()

    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(within(dialog).getByText(/None yet/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Libraries/ })).toHaveTextContent(/^Libraries$/)
  })
})
