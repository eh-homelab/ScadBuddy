import { fireEvent, screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import type { ProjectChoices } from '../../api/types'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { RememberedChoicesPanel } from './RememberedChoicesPanel'

describe('RememberedChoicesPanel · model projects (#1660)', () => {
  it('lists the project each model and preset was filed in, and forgets one', async () => {
    const forgot: { slug: string; body: unknown }[] = []
    server.use(
      http.get('/api/v1/settings/remembered', () =>
        HttpResponse.json({
          model_projects: {
            critter: { project_id: 1 },
            'critter/template-fish': { project_id: 2, preset_name: 'Fish' },
          },
        }),
      ),
      http.put('/api/v1/print/models/:slug/project', async ({ params, request }) => {
        forgot.push({ slug: String(params['slug']), body: await request.json() })
        return HttpResponse.json({ projects: [] })
      }),
    )
    const projects = {
      projects: [
        { id: 1, name: 'Critters' },
        { id: 2, name: 'Fish' },
      ],
    } as ProjectChoices
    renderPage(<RememberedChoicesPanel targets={undefined} projects={projects} />)
    expect(await screen.findByText('critter · Fish')).toBeInTheDocument()
    expect(screen.getByText('Critters')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Forget project for critter · Fish' }))
    await waitFor(() =>
      expect(forgot).toEqual([{ slug: 'critter', body: { preset_id: 'template-fish', project_id: null } }]),
    )
  })

  it("names a preset by its name now, the remembered one only when it is gone", async () => {
    server.use(
      http.get('/api/v1/settings/remembered', () =>
        HttpResponse.json({
          model_projects: {
            'critter/saved-1': { project_id: 1, preset_name: 'Fish' },
            'critter/saved-2': { project_id: 1, preset_name: 'Snake' },
          },
        }),
      ),
      http.get('/api/v1/models/critter/presets', () =>
        HttpResponse.json([{ id: 'saved-1', name: 'Big fish', params: {}, source: 'saved' }]),
      ),
    )
    renderPage(<RememberedChoicesPanel targets={undefined} projects={null} />)
    expect(await screen.findByText('critter · Big fish')).toBeInTheDocument()
    // Deleted since: the name it was remembered with.
    expect(screen.getByText('critter · Snake')).toBeInTheDocument()
  })
})
