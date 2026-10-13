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

  it('names presets from the one remembered read, never a preset list per model', async () => {
    // The server names each preset by its name now and drops one that is gone, so the
    // panel needs no `listPresets` per model (#2142 review).
    const presetReads: string[] = []
    server.use(
      http.get('/api/v1/settings/remembered', () =>
        HttpResponse.json({
          model_projects: {
            'critter/saved-1': { project_id: 1, preset_name: 'Big fish' },
            'gear/template-small': { project_id: 1 },
            'box/saved-9': { project_id: 1, preset_name: 'Lid' },
          },
        }),
      ),
      http.get('/api/v1/models/:slug/presets', ({ params }) => {
        presetReads.push(String(params['slug']))
        return HttpResponse.json([])
      }),
    )
    renderPage(<RememberedChoicesPanel targets={undefined} projects={null} />)
    expect(await screen.findByText('critter · Big fish')).toBeInTheDocument()
    expect(screen.getByText('box · Lid')).toBeInTheDocument()
    // No name sent: the preset's id.
    expect(screen.getByText('gear · template-small')).toBeInTheDocument()
    expect(presetReads).toEqual([])
  })
})
