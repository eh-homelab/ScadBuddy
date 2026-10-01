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

function select(): HTMLSelectElement {
  return screen.getByTestId('project-select')
}

/** The list lands before anything else can be asserted. */
async function listed() {
  await screen.findByTestId('project-select')
  await waitFor(() => expect(within(select()).getAllByRole('option').length).toBeGreaterThan(2))
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
    mount()
    await listed()

    // The folder is named first because it is what decides whether the send has anywhere
    // to go: Bambuddy's project page lists the folder's files, not the project row.
    expect(within(select()).getByRole('option', { name: /Reagan Keychain/ })).toHaveTextContent(
      'Raegan',
    )
    expect(within(select()).getByRole('option', { name: /Reagan Keychain/ })).toHaveTextContent(
      '1 archived',
    )
    expect(within(select()).getByRole('option', { name: /Gridfinity Bins/ })).toHaveTextContent(
      '3 queued',
    )
    expect(within(select()).getByRole('option', { name: 'No project' })).toBeInTheDocument()
  })

  it('warns before the send when the chosen project has no folder yet', async () => {
    const { user } = mount()
    await listed()

    await user.selectOptions(select(), '1')
    expect(screen.queryByTestId('project-no-folder')).not.toBeInTheDocument()

    // A project made in Bambuddy rather than here usually has no folder; linking it
    // creates one rather than refusing, and the picker says so first.
    await user.selectOptions(select(), '2')
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

    await user.selectOptions(select(), 'new')
    await user.type(screen.getByTestId('new-project-name'), 'Workshop Bins')
    await user.type(screen.getByLabelText('Colour'), '#ef4444')
    await user.click(screen.getByTestId('create-project'))

    await waitFor(() =>
      expect(select().selectedOptions[0]).toHaveTextContent(/Workshop Bins · Workshop Bins/),
    )
    // Bambuddy's ProjectCreate also carries target_count, due_date and budget; inventing
    // values for them would put numbers nobody chose on its project page.
    expect(posted).toEqual([{ name: 'Workshop Bins', description: null, colour: '#ef4444' }])
  })

  it('opens on the project the last send went to', async () => {
    withLastProject(1)
    mount()
    await listed()

    // ScadBuddy remembers one id so the picker opens where it was left. It models no
    // relationship between a model and a project — which prints belong to a project is
    // on the project's own page, and a second answer here would go stale.
    await waitFor(() => expect(select()).toHaveValue('1'))
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
    await waitFor(() => expect(screen.getByTestId('first')).toHaveValue('1'))

    await user.selectOptions(select(), 'new')
    await user.type(screen.getByTestId('new-project-name'), 'Workshop Bins')
    await user.click(screen.getByTestId('create-project'))
    await waitFor(() => expect(select().selectedOptions[0]).toHaveTextContent(/Workshop Bins/))
    const created = select().value

    // The first picker's list predates the project, so it re-reads; the value is kept.
    await new Promise((resolve) => setTimeout(resolve, 200))
    expect(select()).toHaveValue(created)
    expect(screen.getByTestId('first')).toHaveValue(created)
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
    expect(select()).toHaveValue('')
  })

  it('writes nothing of its own when a project is chosen', async () => {
    const writes: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method !== 'GET' && request.url.includes('/print/')) writes.push(request.url)
    })
    const { user } = mount()
    await listed()

    await user.selectOptions(select(), '2')

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
})
