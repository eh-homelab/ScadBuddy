import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { MISSING_REF, models } from '../mocks/fixtures'
import { setMockInvalidLibraries } from '../mocks/handlers'
import { emitRealtime } from '../mocks/realtime'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { ModelLibrariesButton } from './ModelLibrariesButton'

const BOSL2_URL = 'https://github.com/BelfrySCAD/BOSL2.git'

function row(name: string) {
  return screen.getByRole('listitem', { name })
}

/** Records every library PUT/DELETE as `METHOD name?query body`. */
function watchPins() {
  const seen: string[] = []
  server.events.on('request:start', async ({ request }) => {
    const url = new URL(request.url)
    const match = /\/libraries\/([^/]+)$/.exec(url.pathname)
    if (!match) return
    const body = request.method === 'PUT' ? ` ${JSON.stringify(await request.clone().json())}` : ''
    seen.push(`${request.method} ${decodeURIComponent(match[1] ?? '')}${url.search}${body}`)
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

describe('ModelLibrariesButton, invalid entries (#217)', () => {
  const BARE = "model.json names library 'threads' without a pin; pin it again"
  const NAMELESS = 'model.json library an entry is not valid: name: Field required; pin it again'
  const BAD_NAME =
    "model.json library 'bad name' is not valid: name: String should match pattern; pin it again"

  it('lists an entry that is not a pin, says why, and removes it', async () => {
    setMockInvalidLibraries('name-keychain', [
      { name: 'threads', index: 0, problem: BARE },
      { name: null, index: 1, problem: NAMELESS },
    ])
    const seen = watchPins()
    const onSaved = vi.fn()
    const { user } = renderPage(
      <ModelLibrariesButton slug="name-keychain" name="Name Keychain" onSaved={onSaved} />,
    )
    const dialog = await openDialog(user, 'Libraries\\s*2')
    const pinned = await within(dialog).findByRole('list', { name: 'Pinned libraries' })

    const bare = within(pinned).getByRole('listitem', { name: 'Invalid entry 1: threads' })
    expect(bare).toHaveTextContent('Invalid')
    expect(bare).toHaveTextContent(BARE)
    // No name the remove route can take: it says where to fix it instead.
    const nameless = within(pinned).getByRole('listitem', { name: 'Invalid entry 2: unnamed' })
    expect(nameless).toHaveTextContent(NAMELESS)
    expect(within(nameless).queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()

    await user.click(within(bare).getByRole('button', { name: 'Remove' }))

    await waitFor(() =>
      expect(within(pinned).queryByRole('listitem', { name: /threads/ })).not.toBeInTheDocument(),
    )
    expect(
      within(pinned).getByRole('listitem', { name: 'Invalid entry 1: unnamed' }),
    ).toHaveTextContent(NAMELESS)
    expect(seen).toEqual(['DELETE threads?index=0'])
    await user.click(within(dialog).getByRole('button', { name: 'Done' }))
    expect(onSaved).toHaveBeenCalledTimes(1)
  })

  it('gives every invalid entry its own accessible name, and removes only the one clicked', async () => {
    // As the backend's fixture in tests/api/test_libraries.py: two entries with no
    // usable name, and here a name given twice as well.
    const SECOND = "model.json library 'threads' is not valid: commit: Field required; pin it again"
    setMockInvalidLibraries('name-keychain', [
      { name: 'threads', index: 0, problem: BARE },
      { name: 'threads', index: 1, problem: SECOND },
      { name: null, index: 2, problem: NAMELESS },
      { name: null, index: 3, problem: BAD_NAME },
    ])
    const seen = watchPins()
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)
    const dialog = await openDialog(user, 'Libraries\\s*4')
    const pinned = await within(dialog).findByRole('list', { name: 'Pinned libraries' })

    const names = within(pinned)
      .getAllByRole('listitem')
      .map((item) => item.getAttribute('aria-label'))
    expect(names).toEqual([
      'Invalid entry 1: threads',
      'Invalid entry 2: threads',
      'Invalid entry 3: unnamed',
      'Invalid entry 4: unnamed',
    ])
    expect(
      within(pinned).getByRole('listitem', { name: 'Invalid entry 4: unnamed' }),
    ).toHaveTextContent(BAD_NAME)

    const second = within(pinned).getByRole('listitem', { name: 'Invalid entry 2: threads' })
    await user.click(within(second).getByRole('button', { name: 'Remove' }))

    await waitFor(() => expect(within(pinned).getAllByRole('listitem')).toHaveLength(3))
    expect(seen).toEqual(['DELETE threads?index=1'])
    // The other entry of that name stays.
    expect(
      within(pinned).getByRole('listitem', { name: 'Invalid entry 1: threads' }),
    ).toHaveTextContent(BARE)
    expect(pinned).not.toHaveTextContent(SECOND)
  })

  it('says why a 409 was refused on the row and reads the model again', async () => {
    const DETAIL =
      "the model.json of 'name-keychain' is not valid: name: Input should be a valid string"
    server.use(
      http.delete(
        '/api/v1/models/:slug/libraries/:name',
        () =>
          HttpResponse.json(
            { title: 'Invalid Model Metadata', status: 409, detail: DETAIL },
            { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
          ),
        { once: true },
      ),
    )
    setMockInvalidLibraries('name-keychain', [{ name: 'threads', index: 0, problem: BARE }])
    const reads: string[] = []
    server.events.on('request:start', ({ request }) => {
      const { pathname } = new URL(request.url)
      if (request.method === 'GET' && pathname === '/api/v1/models/name-keychain') {
        reads.push(pathname)
      }
    })
    const onSaved = vi.fn()
    const { user } = renderPage(
      <ModelLibrariesButton slug="name-keychain" name="Name Keychain" onSaved={onSaved} />,
    )
    const dialog = await openDialog(user, 'Libraries\\s*1')
    const bare = await within(dialog).findByRole('listitem', { name: 'Invalid entry 1: threads' })
    const before = reads.length

    await user.click(within(bare).getByRole('button', { name: 'Remove' }))

    expect(await within(bare).findByRole('alert')).toHaveTextContent(DETAIL)
    await waitFor(() => expect(reads.length).toBe(before + 1))
    expect(
      within(dialog).getByRole('listitem', { name: 'Invalid entry 1: threads' }),
    ).toHaveTextContent(BARE)
    await user.click(within(dialog).getByRole('button', { name: 'Done' }))
    expect(onSaved).not.toHaveBeenCalled()
  })
  it("keeps a failed remove's error on its own row when the list shifts under it", async () => {
    // Another tab removed the first entry already: the 409 arrives, the model is read
    // again, and the second entry moves up into the first one's position.
    const GONE = "'name-keychain''s entry 0 is no longer an invalid 'threads'; nothing was removed"
    server.use(
      http.delete(
        '/api/v1/models/:slug/libraries/:name',
        () => {
          setMockInvalidLibraries('name-keychain', [{ name: null, index: 0, problem: NAMELESS }])
          return HttpResponse.json(
            { title: 'Conflict', status: 409, detail: GONE },
            { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
          )
        },
        { once: true },
      ),
    )
    setMockInvalidLibraries('name-keychain', [
      { name: 'threads', index: 0, problem: BARE },
      { name: null, index: 1, problem: NAMELESS },
    ])
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)
    const dialog = await openDialog(user, 'Libraries\\s*2')
    const pinned = await within(dialog).findByRole('list', { name: 'Pinned libraries' })
    const bare = within(pinned).getByRole('listitem', { name: 'Invalid entry 1: threads' })

    await user.click(within(bare).getByRole('button', { name: 'Remove' }))

    const moved = await within(pinned).findByRole('listitem', { name: 'Invalid entry 1: unnamed' })
    expect(within(pinned).getAllByRole('listitem')).toHaveLength(1)
    expect(within(moved).queryByRole('alert')).not.toBeInTheDocument()
    expect(moved).not.toHaveTextContent(GONE)
  })
})

describe('ModelLibrariesButton, live (#269)', () => {
  it('shows a library pinned elsewhere while the dialog is open', async () => {
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)
    const dialog = await openDialog(user)
    await within(dialog).findByRole('list', { name: 'Catalogue' })
    expect(within(dialog).queryByRole('list', { name: 'Pinned libraries' })).not.toBeInTheDocument()

    await withBosl2()
    emitRealtime('library.changed', ['libraries', 'model:name-keychain'], {
      slug: 'name-keychain',
      name: 'BOSL2',
    })
    const pinned = await within(dialog).findByRole('list', { name: 'Pinned libraries' })
    expect(within(pinned).getByRole('listitem', { name: 'BOSL2' })).toBeInTheDocument()
  })
  describe('Includes (#1285)', () => {
    const clean = {
      includes: [],
      unresolved: 0,
      fonts: [],
      fonts_checked: true,
      missing_checkouts: [],
      truncated: false,
    }
    const unresolvedBosl2 = {
      file: 'model.scad',
      line: 3,
      kind: 'include',
      target: 'BOSL2/std.scad',
      status: 'unresolved',
      path: null,
      library: null,
      reason: 'no pinned library has it',
      suggestion: {
        name: 'BOSL2',
        source: 'catalogue',
        url: BOSL2_URL,
        ref: 'v2.0.761',
        commit: null,
        has_file: null,
        pinned_by: null,
      },
    }

    /** Answers each check with the next report (the last one repeats), counting them. */
    function reports(...answers: object[]) {
      const asked: string[] = []
      server.use(
        http.post('/api/v1/models/:slug/dependencies', ({ params }) => {
          asked.push(String(params.slug))
          return HttpResponse.json(answers[Math.min(asked.length, answers.length) - 1])
        }),
      )
      return asked
    }

    it('says every include resolves when the check finds nothing', async () => {
      reports({
        ...clean,
        includes: [
          {
            ...unresolvedBosl2,
            status: 'resolved',
            suggestion: null,
            reason: null,
          },
        ],
      })
      const { user } = renderPage(
        <ModelLibrariesButton slug="name-keychain" name="Name Keychain" />,
      )
      const dialog = await openDialog(user)
      const includes = within(dialog).getByRole('region', { name: 'Includes' })
      expect(
        await within(includes).findByText('Every include and use resolves.'),
      ).toBeInTheDocument()
      expect(within(includes).queryByRole('list', { name: 'Unresolved' })).not.toBeInTheDocument()
    })

    it('lists an unresolved include, pins the library it suggests, and checks again', async () => {
      const seen = watchPins()
      const asked = reports({ ...clean, includes: [unresolvedBosl2], unresolved: 1 }, clean)
      const { user } = renderPage(
        <ModelLibrariesButton slug="name-keychain" name="Name Keychain" />,
      )
      const dialog = await openDialog(user)

      const unresolved = await within(dialog).findByRole('list', {
        name: 'Unresolved',
      })
      const include = within(unresolved).getByRole('listitem', {
        name: '<BOSL2/std.scad>',
      })
      expect(include).toHaveTextContent('include <BOSL2/std.scad>')
      expect(include).toHaveTextContent('model.scad:3 — no pinned library has it')
      expect(include).toHaveTextContent('BOSL2 from the catalogue provides it.')

      await user.click(within(include).getByRole('button', { name: 'Pin BOSL2' }))

      expect(
        await within(dialog).findByText('The source has no include or use.'),
      ).toBeInTheDocument()
      expect(seen).toEqual([`PUT BOSL2 ${JSON.stringify({ url: BOSL2_URL, ref: 'v2.0.761' })}`])
      expect(asked.length).toBeGreaterThanOrEqual(2)
      expect(asked.every((slug) => slug === 'name-keychain')).toBe(true)
    })

    it('installs a font the source names that is not installed, then checks again', async () => {
      const font = {
        file: 'model.scad',
        line: 7,
        font: 'Pacifico',
        families: ['Pacifico'],
        missing: ['Pacifico'],
      }
      const asked = reports(
        { ...clean, fonts: [font] },
        { ...clean, fonts: [{ ...font, missing: [] }] },
      )
      const { user } = renderPage(
        <ModelLibrariesButton slug="name-keychain" name="Name Keychain" />,
      )
      const dialog = await openDialog(user)

      const row = await within(dialog).findByRole('listitem', {
        name: 'Font Pacifico',
      })
      expect(row).toHaveTextContent('Not installed')
      const checksBefore = asked.length
      await user.click(within(row).getByRole('button', { name: 'Install Pacifico' }))

      expect(
        await within(dialog).findByText(
          'The source has no include or use, and every font it names is installed.',
        ),
      ).toBeInTheDocument()
      expect(asked.length).toBeGreaterThan(checksBefore)
    })

    it('says why a font could not be installed, on its row', async () => {
      const font = {
        file: 'model.scad',
        line: 2,
        font: 'Playfair Display',
        families: ['Playfair Display'],
        missing: ['Playfair Display'],
      }
      reports({ ...clean, fonts: [font] })
      const { user } = renderPage(
        <ModelLibrariesButton slug="name-keychain" name="Name Keychain" />,
      )
      const dialog = await openDialog(user)
      const row = await within(dialog).findByRole('listitem', {
        name: 'Font Playfair Display',
      })
      await user.click(within(row).getByRole('button', { name: 'Install Playfair Display' }))
      expect(await within(row).findByRole('alert')).toHaveTextContent(/could not be downloaded/)
    })

    it('says which pinned checkouts are missing, that fonts went unchecked, and that the check stopped short', async () => {
      reports({
        ...clean,
        fonts: [
          {
            file: 'model.scad',
            line: 1,
            font: 'Roboto',
            families: ['Roboto'],
            missing: [],
          },
        ],
        fonts_checked: false,
        missing_checkouts: ['BOSL2'],
        truncated: true,
      })
      const { user } = renderPage(
        <ModelLibrariesButton slug="name-keychain" name="Name Keychain" />,
      )
      const dialog = await openDialog(user)
      const includes = within(dialog).getByRole('region', { name: 'Includes' })
      expect(
        await within(includes).findByText(/Not on the volume yet: BOSL2\./),
      ).toBeInTheDocument()
      expect(within(includes).getByText(/The fonts were not checked/)).toBeInTheDocument()
      expect(within(includes).getByText(/The check stopped short/)).toBeInTheDocument()
    })

    it('names the model an installed suggestion is pinned by, and says when its checkout lacks the file', async () => {
      reports({
        ...clean,
        unresolved: 1,
        includes: [
          {
            ...unresolvedBosl2,
            suggestion: {
              ...unresolvedBosl2.suggestion,
              source: 'installed',
              commit: 'abc1234',
              has_file: false,
              pinned_by: 'cable-label',
            },
          },
        ],
      })
      const { user } = renderPage(
        <ModelLibrariesButton slug="name-keychain" name="Name Keychain" />,
      )
      const dialog = await openDialog(user)
      const include = await within(dialog).findByRole('listitem', { name: '<BOSL2/std.scad>' })
      expect(include).toHaveTextContent(
        'BOSL2, as cable-label pins it, would provide it (its checkout here has no such file).',
      )
    })

    it("does not check again on another model's pin signal, only on this model's (#2193)", async () => {
      const asked = reports(clean)
      let modelReads = 0
      server.events.on('request:end', ({ request }) => {
        if (request.method === 'GET' && new URL(request.url).pathname === '/api/v1/models/name-keychain') {
          modelReads += 1
        }
      })
      const { user } = renderPage(
        <ModelLibrariesButton slug="name-keychain" name="Name Keychain" />,
      )
      const dialog = await openDialog(user)
      await within(dialog).findByText('The source has no include or use.')
      const checks = asked.length
      const reads = modelReads

      // Another model's pin: this model is read again, its pins are the same, nothing is checked.
      emitRealtime('library.changed', ['libraries', 'model:cable-label'], {
        slug: 'cable-label',
        name: 'BOSL2',
      })
      await waitFor(() => expect(modelReads).toBeGreaterThan(reads))
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(asked).toHaveLength(checks)

      // This model's pin: checked again.
      await withBosl2()
      emitRealtime('library.changed', ['libraries', 'model:name-keychain'], {
        slug: 'name-keychain',
        name: 'BOSL2',
      })
      await waitFor(() => expect(asked).toHaveLength(checks + 1))
    })

    it('says it is checking again while a re-check runs, keeping the last report', async () => {
      let release: () => void = () => {}
      const held = new Promise<void>((resolve) => {
        release = resolve
      })
      let calls = 0
      server.use(
        http.post('/api/v1/models/:slug/dependencies', async () => {
          calls += 1
          if (calls === 1) return HttpResponse.json({ ...clean, includes: [unresolvedBosl2], unresolved: 1 })
          await held
          return HttpResponse.json(clean)
        }),
      )
      const { user } = renderPage(
        <ModelLibrariesButton slug="name-keychain" name="Name Keychain" />,
      )
      const dialog = await openDialog(user)
      const includes = within(dialog).getByRole('region', { name: 'Includes' })
      const include = await within(includes).findByRole('listitem', { name: '<BOSL2/std.scad>' })
      expect(within(includes).queryByText('Checking again')).not.toBeInTheDocument()

      await user.click(within(include).getByRole('button', { name: 'Pin BOSL2' }))
      expect(await within(includes).findByText('Checking again')).toBeInTheDocument()
      expect(includes).toHaveAttribute('aria-busy', 'true')
      expect(within(includes).getByRole('listitem', { name: '<BOSL2/std.scad>' })).toBeInTheDocument()

      release()
      expect(await within(includes).findByText('The source has no include or use.')).toBeInTheDocument()
      expect(within(includes).queryByText('Checking again')).not.toBeInTheDocument()
      expect(includes).toHaveAttribute('aria-busy', 'false')
    })

    it('says so when the check fails, and the rest of the dialog still works', async () => {
      server.use(
        http.post('/api/v1/models/:slug/dependencies', () =>
          HttpResponse.json({ detail: 'the model source could not be read' }, { status: 500 }),
        ),
      )
      const { user } = renderPage(
        <ModelLibrariesButton slug="name-keychain" name="Name Keychain" />,
      )
      const dialog = await openDialog(user)
      const includes = within(dialog).getByRole('region', { name: 'Includes' })
      expect(await within(includes).findByRole('alert')).toHaveTextContent(
        /Could not check the includes/,
      )
      expect(within(dialog).getByRole('list', { name: 'Catalogue' })).toBeInTheDocument()
    })
  })
})
