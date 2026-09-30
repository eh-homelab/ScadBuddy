import { render, screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes, useLocation } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { setMockMergeFiles } from '../mocks/handlers'
import { server } from '../mocks/server'
import type { UpstreamState } from '../api/types'
import { renderPage } from '../test/utils'
import { COPY, UPSTREAM, duplicateWithUpdate, ours, theirs } from '../test/upstream'
import { UpstreamBadge, UpstreamUpdateButton } from './UpstreamUpdate'

function Where() {
  const { pathname, search } = useLocation()
  return <div data-testid="where">{pathname + search}</div>
}

async function renderButton() {
  const onChanged = vi.fn()
  const state = (await api.getModel(COPY)).upstream_state
  const view = renderPage(
    <Routes>
      <Route
        path="/"
        element={<UpstreamUpdateButton slug={COPY} state={state} onChanged={onChanged} />}
      />
      <Route path="*" element={<Where />} />
    </Routes>,
  )
  return { ...view, onChanged }
}

describe('UpstreamBadge (#160)', () => {
  it.each([
    ['update', 'Update available'],
    ['gone', 'Upstream gone'],
  ] as const)('says %s', (state, text) => {
    render(<UpstreamBadge state={state} />)
    expect(screen.getByText(text)).toBeInTheDocument()
  })

  it.each<UpstreamState | null>(['current', 'dismissed', null])(
    'says nothing when %s',
    (state) => {
      const { container } = render(<UpstreamBadge state={state} />)
      expect(container).toBeEmptyDOMElement()
    },
  )
})

describe('UpstreamUpdateButton (#160)', () => {
  it('shows the upstream diff and a clean merge, and takes it', async () => {
    await duplicateWithUpdate()
    const { user, onChanged } = await renderButton()

    await user.click(screen.getByRole('button', { name: 'Update available' }))
    const dialog = screen.getByRole('dialog', { name: 'Update available' })
    expect(await within(dialog).findByTestId('diff')).toHaveTextContent('+text_size = 16;')
    expect(within(dialog).getByTestId('diff')).toHaveTextContent('-text_size = 14;')
    expect(within(dialog).getByTestId('merge-verdict')).toHaveTextContent('Merges cleanly')
    expect(within(dialog).getByTestId('merge-result')).toHaveTextContent('text_size = 16;')

    await user.click(within(dialog).getByRole('button', { name: 'Take update' }))

    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledExactlyOnceWith('merge'))
    // done() calls onChanged before React commits setOpen(false), so the dialog can still be there.
    await vi.waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect((await api.getModel(COPY)).upstream_state).toBe('current')
    expect(await api.getSource(COPY)).toBe(theirs)
    expect((await api.listVersions(COPY))[0]?.message).toBe(`Merge ${UPSTREAM} into ${COPY}`)
  })

  it("shows the preview's patch, from where the upstream was at base (#236)", async () => {
    // A seeded template linked to its built-in: base is the seed commit at `<slug>`,
    // the built-in lives at `_builtin/<slug>`. The upstream's own version diff cannot
    // see across that move, so the dialog shows the patch the preview carries.
    await duplicateWithUpdate()
    const status = await api.getUpstream(COPY)
    const patch = [
      `--- a/${UPSTREAM}/model.scad`,
      `+++ b/${UPSTREAM}/model.scad`,
      '@@ -1 +1 @@',
      '-size = 10;',
      '+size = 12;',
      '',
    ].join('\n')
    const versionDiff = vi.fn()
    server.use(
      http.get('/api/v1/models/:slug/upstream', () =>
        HttpResponse.json({ ...status, preview: { ...status.preview, patch } }),
      ),
      http.get('/api/v1/models/:slug/versions/:commit/diff', () => {
        versionDiff()
        return HttpResponse.json({ patch: 'new file mode 100644' })
      }),
    )
    const { user } = await renderButton()

    await user.click(screen.getByRole('button', { name: 'Update available' }))
    const diff = await within(screen.getByRole('dialog')).findByTestId('diff')

    expect(diff).toHaveTextContent('-size = 10;')
    expect(diff).toHaveTextContent('+size = 12;')
    expect(diff).not.toHaveTextContent('new file')
    expect(versionDiff).not.toHaveBeenCalled()
  })

  it('says a conflicted merge will open in the editor, and opens it', async () => {
    await duplicateWithUpdate({ conflict: true })
    const { user, onChanged } = await renderButton()

    await user.click(screen.getByRole('button', { name: 'Update available' }))
    const dialog = screen.getByRole('dialog')
    expect(await within(dialog).findByTestId('merge-verdict')).toHaveTextContent(
      '1 conflict with your edits',
    )
    expect(within(dialog).getByTestId('merge-result')).toHaveTextContent('<<<<<<<')

    await user.click(within(dialog).getByRole('button', { name: 'Take update' }))

    expect(await screen.findByTestId('where')).toHaveTextContent(`/m/${COPY}/source?merge`)
    expect(onChanged).not.toHaveBeenCalled()
    // Nothing was written: the resolution is the editor's to save.
    expect(await api.getSource(COPY)).toBe(ours)
  })

  it('lists no other files when only model.scad changed (#237)', async () => {
    await duplicateWithUpdate()
    const { user } = await renderButton()

    await user.click(screen.getByRole('button', { name: 'Update available' }))
    const dialog = screen.getByRole('dialog')
    await within(dialog).findByTestId('merge-result')

    expect(dialog).not.toHaveTextContent('Also takes:')
    expect(dialog).not.toHaveTextContent('Keeps yours')
  })

  it('lists the other files the update takes (#237)', async () => {
    await duplicateWithUpdate()
    setMockMergeFiles(COPY, { taken: ['README.md', 'presets.json'], kept: [] })
    const { user } = await renderButton()

    await user.click(screen.getByRole('button', { name: 'Update available' }))
    const dialog = screen.getByRole('dialog')
    await within(dialog).findByTestId('merge-result')

    expect(within(dialog).getByText('Also takes:')).toHaveTextContent(
      'Also takes: README.md, presets.json',
    )
    expect(dialog).not.toHaveTextContent('Keeps yours')
  })

  it('lists the other files changed on both sides, which stay yours (#237)', async () => {
    await duplicateWithUpdate()
    setMockMergeFiles(COPY, { taken: [], kept: ['README.md'] })
    const { user } = await renderButton()

    await user.click(screen.getByRole('button', { name: 'Update available' }))
    const dialog = screen.getByRole('dialog')
    await within(dialog).findByTestId('merge-result')

    expect(within(dialog).getByText('Keeps yours, changed on both sides:')).toHaveTextContent(
      'Keeps yours, changed on both sides: README.md',
    )
    expect(dialog).not.toHaveTextContent('Also takes:')
  })

  it('dismisses the update on Not now', async () => {
    await duplicateWithUpdate()
    const { user, onChanged } = await renderButton()

    await user.click(screen.getByRole('button', { name: 'Update available' }))
    const dialog = screen.getByRole('dialog')
    await within(dialog).findByTestId('merge-result')
    await user.click(within(dialog).getByRole('button', { name: 'Not now' }))

    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledExactlyOnceWith('dismiss'))
    expect((await api.getModel(COPY)).upstream_state).toBe('dismissed')
    expect(await api.getSource(COPY)).not.toBe(theirs)
  })

  it('keeps a dismissed update reachable, and takes it (#235)', async () => {
    await duplicateWithUpdate()
    await api.dismissUpstream(COPY)
    const { user, onChanged } = await renderButton()

    expect(screen.queryByRole('button', { name: 'Update available' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Update dismissed — review' }))
    const dialog = screen.getByRole('dialog', { name: 'Dismissed update' })
    expect(dialog).toHaveTextContent('you can still take it')
    expect(await within(dialog).findByTestId('diff')).toHaveTextContent('+text_size = 16;')
    expect(within(dialog).getByTestId('merge-result')).toHaveTextContent('text_size = 16;')
    // Already dismissed: it can be taken or left, not dismissed again.
    expect(within(dialog).queryByRole('button', { name: 'Not now' })).not.toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Take update' }))

    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledExactlyOnceWith('merge'))
    expect((await api.getModel(COPY)).upstream_state).toBe('current')
    expect(await api.getSource(COPY)).toBe(theirs)
  })

  it('closes a dismissed update without writing anything (#235)', async () => {
    await duplicateWithUpdate()
    await api.dismissUpstream(COPY)
    const { user, onChanged } = await renderButton()

    await user.click(screen.getByRole('button', { name: 'Update dismissed — review' }))
    const dialog = screen.getByRole('dialog', { name: 'Dismissed update' })
    await within(dialog).findByTestId('merge-result')
    await user.click(within(dialog).getByRole('button', { name: 'Close' }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(onChanged).not.toHaveBeenCalled()
    expect((await api.getModel(COPY)).upstream_state).toBe('dismissed')
  })

  it('opens a conflicted dismissed update in the editor (#235)', async () => {
    await duplicateWithUpdate({ conflict: true })
    await api.dismissUpstream(COPY)
    const { user } = await renderButton()

    await user.click(screen.getByRole('button', { name: 'Update dismissed — review' }))
    const dialog = screen.getByRole('dialog')
    await within(dialog).findByTestId('merge-verdict')
    await user.click(within(dialog).getByRole('button', { name: 'Take update' }))

    expect(await screen.findByTestId('where')).toHaveTextContent(`/m/${COPY}/source?merge`)
  })

  it('offers a newer update when a dismissed one was superseded (#404)', async () => {
    await duplicateWithUpdate()
    const newer = await api.getUpstream(COPY)
    await api.dismissUpstream(COPY)
    const { user } = await renderButton()
    // The server now reports an update that is not dismissed.
    server.use(http.get('/api/v1/models/:slug/upstream', () => HttpResponse.json(newer)))

    await user.click(screen.getByRole('button', { name: 'Update dismissed — review' }))
    const dialog = await screen.findByRole('dialog', { name: 'Update available' })
    expect(await within(dialog).findByTestId('diff')).toHaveTextContent('+text_size = 16;')
    expect(dialog).not.toHaveTextContent('There is no update to take any more')
    expect(dialog).not.toHaveTextContent('you can still take it')
    expect(within(dialog).getByRole('button', { name: 'Not now' })).toBeEnabled()
    expect(within(dialog).getByRole('button', { name: 'Take update' })).toBeEnabled()
  })

  it('says there is no update when the server reports current (#404)', async () => {
    await duplicateWithUpdate()
    const status = await api.getUpstream(COPY)
    const { user } = await renderButton()
    server.use(
      http.get('/api/v1/models/:slug/upstream', () =>
        HttpResponse.json({ ...status, state: 'current', preview: null }),
      ),
    )

    await user.click(screen.getByRole('button', { name: 'Update available' }))
    const dialog = screen.getByRole('dialog')
    expect(
      await within(dialog).findByText('There is no update to take any more.'),
    ).toBeInTheDocument()
    expect(dialog).toHaveAccessibleName('No update')
    expect(dialog).toHaveTextContent(`This copy is up to date with ${UPSTREAM}.`)
    expect(dialog).not.toHaveTextContent('has changed')
    expect(within(dialog).queryByTestId('merge-result')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Take update' })).toBeDisabled()
    expect(within(dialog).queryByRole('button', { name: 'Not now' })).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Close' })).toBeEnabled()
  })

  it('says the upstream is gone when it went while the dialog loaded (#404)', async () => {
    await duplicateWithUpdate()
    const status = await api.getUpstream(COPY)
    const { user } = await renderButton()
    let answer: (() => void) | undefined
    const answered = new Promise<void>((resolve) => (answer = resolve))
    server.use(
      http.get('/api/v1/models/:slug/upstream', async () => {
        await answered
        return HttpResponse.json({ ...status, state: 'gone', revision: null, preview: null })
      }),
    )

    await user.click(screen.getByRole('button', { name: 'Update available' }))
    // In flight: framed by the state it was opened from.
    const dialog = screen.getByRole('dialog', { name: 'Update available' })
    expect(within(dialog).getByText('Working out the merge')).toBeInTheDocument()
    answer?.()

    expect(
      await within(dialog).findByText('There is no update to take any more.'),
    ).toBeInTheDocument()
    expect(dialog).toHaveAccessibleName('No update')
    expect(dialog).toHaveTextContent(`${UPSTREAM} no longer exists.`)
    expect(dialog).not.toHaveTextContent('has changed')
    expect(within(dialog).getByRole('button', { name: 'Take update' })).toBeDisabled()
    expect(within(dialog).queryByRole('button', { name: 'Not now' })).not.toBeInTheDocument()
  })

  it('offers Detach for an upstream that is gone', async () => {
    await api.duplicateModel(UPSTREAM, 'Keychain for Nova')
    await api.deleteModel(UPSTREAM, true)
    const { user, onChanged } = await renderButton()

    await user.click(screen.getByRole('button', { name: 'Upstream gone' }))
    const dialog = screen.getByRole('dialog', { name: 'Upstream gone' })
    expect(dialog).toHaveTextContent('no longer exists')
    await user.click(within(dialog).getByRole('button', { name: 'Detach' }))

    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledExactlyOnceWith('detach'))
    const detached = await api.getModel(COPY)
    expect(detached.upstream).toBeNull()
    expect(detached.upstream_state).toBeUndefined()
  })

  it('shows nothing for a duplicate that is current', async () => {
    await api.duplicateModel(UPSTREAM, 'Keychain for Nova')
    await renderButton()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
