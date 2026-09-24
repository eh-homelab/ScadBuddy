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
        slug="name-keychain"
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

/** The model already files its prints under `Reagan Keychain`. */
function withModelProject(projectId: number | null = 1) {
  server.use(
    http.get('/api/v1/print/projects', () =>
      HttpResponse.json({ projects: fixtures.projectViews, model_project_id: projectId }),
    ),
  )
}

/** Every `PUT /print/models/{slug}/project` body, in order. */
function watchRemembered(): { project_id: number | null }[] {
  const writes: { project_id: number | null }[] = []
  server.events.on('request:start', async ({ request }) => {
    if (request.method === 'PUT' && request.url.endsWith('/project')) {
      writes.push((await request.clone().json()) as { project_id: number | null })
    }
  })
  return writes
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

  it('remembers the project for this model when the tick is a real change', async () => {
    const writes = watchRemembered()
    const { user } = mount()
    await listed()

    // Nothing to remember until a project is in view.
    expect(screen.getByTestId('remember-project')).toBeDisabled()

    await user.selectOptions(select(), '1')
    await user.click(screen.getByTestId('remember-project'))

    await waitFor(() => expect(writes).toEqual([{ project_id: 1 }]))
  })

  it('does not re-point an existing model project at whatever is selected next', async () => {
    withModelProject(1)
    const writes = watchRemembered()
    const { user } = mount()
    await listed()

    // Opened on the model's project, so the tick says "this one *is* it".
    await waitFor(() => expect(screen.getByTestId('remember-project')).toBeChecked())

    await user.selectOptions(select(), '2')

    // Printing to a different project once says nothing about where this model belongs,
    // so the tick does not follow the selection and nothing is written — in either
    // direction: the stored project is neither moved nor cleared.
    expect(screen.getByTestId('remember-project')).not.toBeChecked()
    expect(writes).toEqual([])
  })

  it('clears the model project only when the user unticks the one that is stored', async () => {
    withModelProject(1)
    const writes = watchRemembered()
    const { user } = mount()
    await listed()
    await waitFor(() => expect(screen.getByTestId('remember-project')).toBeChecked())

    await user.click(screen.getByTestId('remember-project'))

    await waitFor(() => expect(writes).toEqual([{ project_id: null }]))
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
