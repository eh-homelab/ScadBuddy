import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProjectRequest } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
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
