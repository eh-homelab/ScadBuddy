import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { ProjectRequest } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { useProjectList } from '../lib/projects'
import { chooseParent, chooseProject, openPicker, pickerValue } from '../test/projectPicker'
import { ProjectPicker } from './ProjectPicker'

/**
 * The picker is controlled — the parent owns the chosen id because the run request
 * carries it — so the tests mount it the way `PrintPicker` will: local state, seeded from
 * `onLoaded` and moved by `onChange`.
 */
function mount(onChange: (projectId: number | null) => void = vi.fn()) {
  function Harness() {
    const [value, setValue] = useState<number | null>(null)
    return (
      <ProjectPicker
        value={value}
        onChange={(projectId) => {
          setValue(projectId)
          onChange(projectId)
        }}
        onLoaded={setValue}
      />
    )
  }
  return renderPage(<Harness />)
}

function select(): HTMLElement {
  return screen.getByTestId('project-select')
}

/** The list lands before anything else can be asserted. */
async function listed() {
  await screen.findByTestId('project-select')
}

/** The last send went to `Reagan Keychain`, so the picker opens on it. */
function withLastProject(projectId: number | null = 1) {
  server.use(
    http.get('/api/v1/print/projects', () =>
      HttpResponse.json({ projects: fixtures.projectViews, last_project_id: projectId }),
    ),
  )
}

describe('ProjectPicker', () => {
  beforeEach(() => resetMockState())

  it('lists each project with the library folder that belongs to it', async () => {
    const { user } = mount()
    await listed()
    const list = await openPicker(user, select())

    // The folder is named first because it is what decides whether the send has anywhere
    // to go: Bambuddy's project page lists the folder's files, not the project row.
    expect(within(list).getByRole('option', { name: /Reagan Keychain/ })).toHaveTextContent(
      'Raegan',
    )
    expect(within(list).getByRole('option', { name: /Reagan Keychain/ })).toHaveTextContent(
      '1 archived',
    )
    expect(within(list).getByRole('option', { name: /Gridfinity Bins/ })).toHaveTextContent(
      '3 queued',
    )
    expect(within(list).getByRole('option', { name: 'No project' })).toBeInTheDocument()
  })

  it('finds a project by a misspelt or partial name (#3651)', async () => {
    const { user } = mount()
    await listed()
    const list = await openPicker(user, select())

    await user.type(within(list).getByPlaceholderText('Search projects…'), 'gridfinty')
    await waitFor(() => expect(within(list).queryByRole('option', { name: /Reagan Keychain/ })).toBeNull())
    await user.click(within(list).getByRole('option', { name: /Gridfinity Bins/ }))
    expect(pickerValue(select())).toBe('2')
  })

  it('lists the most recently chosen project first, or sorts by name (#3651)', async () => {
    window.localStorage.clear()
    const { user } = mount()
    await listed()
    await chooseProject(user, select(), 1)

    let list = await openPicker(user, select())
    const names = () =>
      within(list)
        .getAllByRole('option')
        .map((option) => option.getAttribute('data-testid'))
        .filter((testId) => testId?.startsWith('project-option-') && testId !== 'project-option-none')
    expect(names()[0]).toBe('project-option-1')

    await user.click(within(list).getByRole('radio', { name: 'Name' }))
    expect(names()[0]).toBe('project-option-2')
    await user.keyboard('{Escape}')
    list = await openPicker(user, select())
    // The order chosen is kept for the next time.
    expect(within(list).getByRole('radio', { name: 'Name' })).toHaveAttribute('aria-checked', 'true')
    window.localStorage.clear()
  })

  it('offers library folders beside projects, each labelled, and chooses one (#3651)', async () => {
    const chosen: { project: number | null; folder: number | null }[] = []
    function Harness() {
      const [value, setValue] = useState<number | null>(null)
      const [folderId, setFolderId] = useState<number | null>(null)
      return (
        <ProjectPicker
          value={value}
          onChange={(projectId) => {
            setValue(projectId)
            chosen.push({ project: projectId, folder: folderId })
          }}
          folder={{
            value: folderId,
            onChange: (next) => {
              setFolderId(next)
              if (next !== null) setValue(null)
            },
          }}
        />
      )
    }
    const { user } = renderPage(<Harness />)
    await listed()
    const list = await openPicker(user, select())

    expect(within(list).getByTestId('project-option-1')).toHaveTextContent('Project · Raegan')
    expect(within(list).getByTestId('folder-option-4')).toHaveTextContent(/Supplies › Storage.*Folder · 1 file/)
    await user.type(within(list).getByPlaceholderText('Search projects and folders…'), 'storag')
    await waitFor(() => expect(within(list).queryByTestId('project-option-1')).toBeNull())
    await user.click(within(list).getByTestId('folder-option-4'))

    expect(pickerValue(select())).toBe('folder-4')
    expect(select()).toHaveTextContent('Folder: Supplies › Storage')
    // A project chosen after it clears the folder.
    await chooseProject(user, select(), 2)
    expect(pickerValue(select())).toBe('2')
  })

  it('opens sorted as Settings says, last touched first by default (#3651)', async () => {
    server.use(
      http.get('/api/v1/print/projects', () =>
        HttpResponse.json({
          // Touched later than any choice this page made in another test.
          projects: [
            { ...fixtures.projectViews[0]!, touched_at: '2099-01-01T00:00:00Z' },
            { ...fixtures.projectViews[1]!, touched_at: '2099-02-01T00:00:00Z' },
          ],
          folders: fixtures.destinationFolders,
          last_project_id: null,
          default_project_id: null,
          sort: 'modified',
        }),
      ),
    )
    const { user } = mount()
    await listed()
    const list = await openPicker(user, select())
    const order = () =>
      within(list)
        .getAllByRole('option')
        .map((option) => option.getAttribute('data-testid'))
        .filter((testId) => testId?.startsWith('project-option-') && testId !== 'project-option-none')

    // Reagan Keychain's folder was written to after Gridfinity Bins was made.
    expect(within(list).getByRole('radio', { name: 'Modified' })).toHaveAttribute('aria-checked', 'true')
    expect(order()).toEqual(['project-option-1', 'project-option-2'])
    // Gridfinity Bins was printed into more recently.
    await user.click(within(list).getByRole('radio', { name: 'Last touched' }))
    expect(order()).toEqual(['project-option-2', 'project-option-1'])
  })

  it('warns before the send when the chosen project has no folder yet', async () => {
    const { user } = mount()
    await listed()

    await chooseProject(user, select(), 1)
    expect(screen.queryByTestId('project-no-folder')).not.toBeInTheDocument()

    // A project made in Bambuddy rather than here usually has no folder; linking it
    // creates one rather than refusing, and the picker says so first.
    await chooseProject(user, select(), 2)
    expect(await screen.findByTestId('project-no-folder')).toHaveTextContent('Gridfinity Bins')
  })

  it('creates a project and selects it, sending only the fields ScadBuddy has an opinion about', async () => {
    const posted: ProjectRequest[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && request.url.endsWith('/print/projects')) {
        posted.push((await request.clone().json()) as ProjectRequest)
      }
    })
    const { user } = mount()
    await listed()

    await chooseProject(user, select(), 'new')
    await user.type(screen.getByTestId('new-project-name'), 'Workshop Bins')
    await user.type(screen.getByLabelText('Colour'), '#ef4444')
    await user.click(screen.getByTestId('create-project'))

    await waitFor(() =>
      expect(select()).toHaveTextContent(/Workshop Bins · Workshop Bins/),
    )
    // Bambuddy's ProjectCreate also carries target_count, due_date and budget; inventing
    // values for them would put numbers nobody chose on its project page.
    expect(posted).toEqual([{ name: 'Workshop Bins', description: null, colour: '#ef4444' }])
  })

  it('nests a new project under a chosen parent, with None the default (#930)', async () => {
    const posted: ProjectRequest[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && request.url.endsWith('/print/projects')) {
        posted.push((await request.clone().json()) as ProjectRequest)
      }
    })
    const { user } = mount()
    await listed()

    await chooseProject(user, select(), 'new')
    const parent = screen.getByTestId('new-project-parent')
    expect(pickerValue(parent)).toBe('')
    expect(parent).toHaveTextContent('None')
    await user.type(screen.getByTestId('new-project-name'), 'Tags')
    await chooseParent(user, 'Reagan Keychain')
    await user.click(screen.getByTestId('create-project'))

    await waitFor(() =>
      expect(select()).toHaveTextContent(/^Reagan Keychain › Tags · /),
    )
    expect(posted).toEqual([{ name: 'Tags', description: null, colour: null, parent_id: 1 }])
  })

  it('offers the project in view as the parent without choosing it (#930)', async () => {
    withLastProject(1)
    const { user } = mount()
    await listed()
    await waitFor(() => expect(pickerValue(select())).toBe('1'))

    await chooseProject(user, select(), 'new')
    const parent = screen.getByTestId('new-project-parent')
    expect(pickerValue(parent)).toBe('')
    await user.click(screen.getByTestId('suggested-parent'))
    expect(pickerValue(screen.getByTestId('new-project-parent'))).toBe('1')
  })

  it('reopens the form on None after a cancelled one chose a parent (#930 review)', async () => {
    const { user } = mount()
    await listed()

    await chooseProject(user, select(), 'new')
    await chooseParent(user, 'Gridfinity Bins')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await chooseProject(user, select(), 'new')

    expect(pickerValue(screen.getByTestId('new-project-parent'))).toBe('')
  })

  it('has the mock refuse a parent the backend refuses (#930)', async () => {
    // A link never re-parents, and a linked folder is not moved under the parent.
    await expect(api.createProject({ project_id: 1, parent_id: 2 })).rejects.toMatchObject({
      status: 400,
    })
    await expect(
      api.createProject({ name: 'Tags', parent_id: 1, folder_id: 2 }),
    ).rejects.toMatchObject({ status: 400 })
  })

  it('shows a child project under its parent as a breadcrumb (#930)', async () => {
    server.use(
      http.get('/api/v1/print/projects', () =>
        HttpResponse.json({
          projects: [
            ...fixtures.projectViews,
            { ...fixtures.projectViews[1]!, id: 3, name: 'Tags', parent_id: 1 },
          ],
          last_project_id: null,
        }),
      ),
    )
    const { user } = mount()
    await listed()
    const list = await openPicker(user, select())
    expect(within(list).getByTestId('project-option-3')).toHaveTextContent(/^Reagan Keychain › Tags/)
    await user.click(within(list).getByTestId('project-option-3'))
    expect(select()).toHaveTextContent(/^Reagan Keychain › Tags · /)
  })

  it('disables the select while its own "Create project" is in flight (#710 review)', async () => {
    // Its own `saving`, not the parent's `disabled`: a reselection here would be silently
    // reverted once the pending create resolves and moves `value` again.
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.post('/api/v1/print/projects', async () => {
        await gate
        return undefined
      }),
    )
    const { user } = mount()
    await listed()

    await chooseProject(user, select(), 'new')
    await user.type(screen.getByTestId('new-project-name'), 'Workshop Bins')
    await user.click(screen.getByTestId('create-project'))
    await waitFor(() => expect(select()).toBeDisabled())

    release()
    await waitFor(() => expect(select()).toBeEnabled())
    expect(select()).toHaveTextContent(/Workshop Bins/)
  })

  it('opens on the project the last send went to', async () => {
    withLastProject(1)
    mount()
    await listed()

    // ScadBuddy remembers one id so the picker opens where it was left. It keeps no
    // project membership — which prints belong to a project is on the project's own
    // page, and a second answer here would go stale.
    await waitFor(() => expect(pickerValue(select())).toBe('1'))
  })

  it('re-reads a list missing the value without moving the value back to the remembered one', async () => {
    // The remembered project stays 1, as while the PUT for the new choice is in flight;
    // the re-read lists the new project (the mock's own list) but still remembers 1.
    await api.rememberProject(1)
    function Harness() {
      const [value, setValue] = useState<number | null>(null)
      return (
        <>
          <ProjectPicker value={value} onChange={setValue} onLoaded={setValue} testId="first" id="first" />
          <ProjectPicker value={value} onChange={setValue} onLoaded={setValue} />
        </>
      )
    }
    const { user } = renderPage(<Harness />)
    await listed()
    await waitFor(() => expect(pickerValue(screen.getByTestId('first'))).toBe('1'))

    await chooseProject(user, select(), 'new')
    await user.type(screen.getByTestId('new-project-name'), 'Workshop Bins')
    await user.click(screen.getByTestId('create-project'))
    await waitFor(() => expect(select()).toHaveTextContent(/Workshop Bins/))
    const created = pickerValue(select())

    // The first picker's list predates the project, so it re-reads; the value is kept.
    await new Promise((resolve) => setTimeout(resolve, 200))
    expect(pickerValue(select())).toBe(created)
    expect(pickerValue(screen.getByTestId('first'))).toBe(created)
  })

  it('re-reads a missing project once for pickers sharing a list, together or one after another', async () => {
    // 99 is remembered but not listed: each picker notices it is missing.
    withLastProject(99)
    const fetched = vi.spyOn(api, 'getProjects')
    function Harness() {
      const [value, setValue] = useState<number | null>(null)
      const [second, setSecond] = useState(false)
      const list = useProjectList(setValue)
      return (
        <>
          <ProjectPicker id="a" testId="picker-a" value={value} onChange={setValue} list={list} />
          <ProjectPicker id="b" testId="picker-b" value={value} onChange={setValue} list={list} />
          <button type="button" onClick={() => setSecond(true)}>
            open
          </button>
          {second && (
            <ProjectPicker id="c" testId="picker-c" value={value} onChange={setValue} list={list} />
          )}
        </>
      )
    }
    const { user } = renderPage(<Harness />)
    await screen.findByTestId('picker-b')
    // The first load, then one re-read for 99 shared by both mounted pickers.
    await waitFor(() => expect(fetched).toHaveBeenCalledTimes(2))

    // A picker mounted later (the print dialog's) does not re-read 99 again.
    await user.click(screen.getByRole('button', { name: 'open' }))
    await screen.findByTestId('picker-c')
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(fetched).toHaveBeenCalledTimes(2)
  })

  it('opens unset when nothing has been sent yet', async () => {
    withLastProject(null)
    mount()
    await listed()
    expect(pickerValue(select())).toBe('')
  })

  it('writes nothing of its own when a project is chosen', async () => {
    const writes: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method !== 'GET' && request.url.includes('/print/')) writes.push(request.url)
    })
    const { user } = mount()
    await listed()

    await chooseProject(user, select(), 2)

    // The choice rides on the run request; there is no per-model store to update.
    expect(writes).toEqual([])
  })

  it('reports a refused list as a message rather than an empty control', async () => {
    const detail = "Bambuddy refused the API key. The key needs the 'Manage Projects' scope"
    server.use(
      http.get('/api/v1/print/projects', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Forbidden', status: 403, detail },
          { status: 403, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    mount()

    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
  })

  it('offers a Retry after a failed read, which loads the list and the last project (#1045)', async () => {
    let calls = 0
    server.use(
      http.get('/api/v1/print/projects', () => {
        calls += 1
        if (calls === 1) return new HttpResponse('upstream request timeout', { status: 504 })
        return HttpResponse.json({ projects: fixtures.projectViews, last_project_id: 1 })
      }),
    )
    const { user } = mount()

    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 504')
    await user.click(screen.getByRole('button', { name: 'Retry' }))

    await listed()
    expect(pickerValue(select())).toBe('1')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(calls).toBe(2)
  })

  it('keeps a project created while the list was failed when Retry then loads it (#1045)', async () => {
    const created = { ...fixtures.projectViews[0]!, id: 99, name: 'Workshop Bins' }
    let calls = 0
    server.use(
      http.get('/api/v1/print/projects', () => {
        calls += 1
        if (calls === 1) return new HttpResponse('upstream request timeout', { status: 504 })
        return HttpResponse.json({ projects: [...fixtures.projectViews, created], last_project_id: 1 })
      }),
      http.post('/api/v1/print/projects', () => HttpResponse.json(created, { status: 201 })),
    )
    const onChange = vi.fn()
    const { user } = mount(onChange)
    await screen.findByRole('alert')

    await chooseProject(user, select(), 'new')
    await user.type(screen.getByTestId('new-project-name'), 'Workshop Bins')
    await user.click(screen.getByTestId('create-project'))
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith(99))
    await user.click(screen.getByRole('button', { name: 'Retry' }))

    await listed()
    // The remembered project does not replace the one just made.
    expect(pickerValue(select())).toBe('99')
  })
})
