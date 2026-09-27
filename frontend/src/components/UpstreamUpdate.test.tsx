import { render, screen, within } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
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

    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledOnce())
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect((await api.getModel(COPY)).upstream_state).toBe('current')
    expect(await api.getSource(COPY)).toBe(theirs)
    expect((await api.listVersions(COPY))[0]?.message).toBe(`Merge ${UPSTREAM} into ${COPY}`)
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

  it('dismisses the update on Not now', async () => {
    await duplicateWithUpdate()
    const { user, onChanged } = await renderButton()

    await user.click(screen.getByRole('button', { name: 'Update available' }))
    const dialog = screen.getByRole('dialog')
    await within(dialog).findByTestId('merge-result')
    await user.click(within(dialog).getByRole('button', { name: 'Not now' }))

    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledOnce())
    expect((await api.getModel(COPY)).upstream_state).toBe('dismissed')
    expect(await api.getSource(COPY)).not.toBe(theirs)
  })

  it('offers Detach for an upstream that is gone', async () => {
    await api.duplicateModel(UPSTREAM, 'Keychain for Nova')
    await api.deleteModel(UPSTREAM)
    const { user, onChanged } = await renderButton()

    await user.click(screen.getByRole('button', { name: 'Upstream gone' }))
    const dialog = screen.getByRole('dialog', { name: 'Upstream gone' })
    expect(dialog).toHaveTextContent('no longer exists')
    await user.click(within(dialog).getByRole('button', { name: 'Detach' }))

    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledOnce())
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
