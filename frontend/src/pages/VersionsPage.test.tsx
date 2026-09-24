import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { versionIds } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { VersionsPage } from './VersionsPage'

function render() {
  return renderPage(<VersionsPage />, {
    route: '/m/name-keychain/versions',
    path: '/m/:slug/versions',
  })
}

async function rows(): Promise<HTMLElement[]> {
  const list = await screen.findByTestId('versions')
  return [...list.children] as HTMLElement[]
}

describe('VersionsPage', () => {
  it('lists every revision newest first, with its message and changed files', async () => {
    render()
    const listed = await rows()

    expect(listed).toHaveLength(3)
    expect(within(listed[0] as HTMLElement).getByText('Edit name-keychain source')).toBeVisible()
    expect(listed[0]).toHaveTextContent(versionIds.raised.slice(0, 7))
    expect(listed[0]).toHaveTextContent('current')
    expect(listed[2]).toHaveTextContent('A model.scad')
  })

  it('shows the newest revision diffed against its parent on open', async () => {
    render()

    const diff = await screen.findByTestId('diff')
    expect(diff).toHaveTextContent('-text_depth = 1.2;')
    expect(diff).toHaveTextContent('+text_depth = 1.6;')
  })

  it('diffs the revision that is selected', async () => {
    const { user } = render()
    const listed = await rows()

    await user.click(within(listed[2] as HTMLElement).getByRole('button', { pressed: false }))

    await waitFor(() =>
      expect(screen.getByTestId('diff')).toHaveTextContent('+name = "Reagan";'),
    )
  })

  it('compares against a chosen revision rather than the parent', async () => {
    const { user } = render()
    await rows()
    await screen.findByTestId('diff')

    await user.selectOptions(
      screen.getByLabelText('Compare with'),
      versionIds.added,
    )

    // Two revisions of distance, so both patches are in the output.
    await waitFor(() => {
      const diff = screen.getByTestId('diff')
      expect(diff).toHaveTextContent('+text_depth = 1.6;')
      expect(diff).toHaveTextContent('Two-colour keychain with raised text.')
    })
  })

  it('restores a revision as a new one at the head of the list', async () => {
    const { user } = render()
    const listed = await rows()

    await user.click(
      within(listed[2] as HTMLElement).getByRole('button', { name: 'Restore this version' }),
    )

    await waitFor(async () => {
      const updated = await rows()
      expect(updated).toHaveLength(4)
      expect(updated[0]).toHaveTextContent(
        `Restore name-keychain to ${versionIds.added.slice(0, 7)}`,
      )
    })
  })

  it('shows the restore it just made, not whatever was selected before it', async () => {
    // A restore only ever ADDS a commit, so the previously selected one is still
    // in the list — nothing re-homes the selection on its own.
    const { user } = render()
    const listed = await rows()
    await screen.findByTestId('diff')

    await user.click(
      within(listed[2] as HTMLElement).getByRole('button', { name: 'Restore this version' }),
    )

    await waitFor(async () => {
      const updated = await rows()
      expect(within(updated[0] as HTMLElement).getByRole('button', { pressed: true })).toBeVisible()
    })
    const updated = await rows()
    expect(
      within(updated[1] as HTMLElement).getByRole('button', { pressed: false }),
    ).toBeVisible()
  })

  it('will not offer to restore the revision the model is already at', async () => {
    render()
    const listed = await rows()

    expect(
      within(listed[0] as HTMLElement).getByRole('button', { name: 'Restore this version' }),
    ).toBeDisabled()
  })

  it('says so when restoring fails and leaves the list alone', async () => {
    server.use(
      http.post('/api/v1/models/:slug/versions/:commit/restore', () =>
        HttpResponse.json(
          { title: 'Restore failed', status: 500, detail: 'the worktree is locked' },
          { status: 500 },
        ),
      ),
    )
    const { user } = render()
    const listed = await rows()

    await user.click(
      within(listed[2] as HTMLElement).getByRole('button', { name: 'Restore this version' }),
    )

    expect(await screen.findByRole('alert')).toHaveTextContent('the worktree is locked')
    expect(await rows()).toHaveLength(3)
  })

  it('reports a model with no history rather than rendering an empty page', async () => {
    server.use(
      http.get('/api/v1/models/:slug/versions', () =>
        HttpResponse.json(
          { title: 'Model history is unavailable', status: 503 },
          { status: 503 },
        ),
      ),
    )
    render()

    expect(await screen.findByRole('alert')).toHaveTextContent('Model history is unavailable')
  })
})
