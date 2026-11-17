import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { getSpool, unbindTag } from '../../api/filament'
import { spoolId, TAG } from '../../mocks/features/filament/seed'
import { renderPage } from '../../test/utils'
import { TagPanel } from './TagPanel'

async function render(number: number) {
  const onChanged = vi.fn()
  const spool = await getSpool(spoolId(number))
  const view = renderPage(<TagPanel spool={spool} onChanged={onChanged} />)
  return { ...view, onChanged }
}

describe('TagPanel', () => {
  it('shows the open tag and takes it off', async () => {
    const { user, onChanged } = await render(4)
    expect(screen.getByText(/A1B2C3D4/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Take off' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect((await getSpool(spoolId(4))).tag).toBeNull()
  })

  it('says whose spool holds the last tag and moves it here', async () => {
    const { user, onChanged } = await render(19)
    await user.click(screen.getByRole('button', { name: 'Tag is on spool #3 — Move it here' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect((await getSpool(spoolId(19))).tag?.id).toBe(TAG.MOVED)
    expect((await getSpool(spoolId(3))).tag).toBeNull()
  })

  it('binds the last tag again when no spool holds it', async () => {
    await unbindTag(TAG.BLACK)
    const { user, onChanged } = await render(4)
    expect(screen.queryByText(/Move it here/)).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Bind tag again' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect((await getSpool(spoolId(4))).tag?.id).toBe(TAG.BLACK)
  })

  it('binds a tag by its UID', async () => {
    const { user, onChanged } = await render(2)
    await user.type(screen.getByLabelText('Tag UID'), '04 AA BB CC')
    await user.click(screen.getByRole('button', { name: 'Bind tag' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect((await getSpool(spoolId(2))).tag?.tag_uid).toBe('04AABBCC')
  })

  it('says the override box only records that the tag was taken', async () => {
    await render(2)
    expect(screen.getByRole('checkbox', { name: /Record that the tag was taken from another spool on purpose/ })).toBeInTheDocument()
  })

  it('refuses a bind with no key', async () => {
    const { user, onChanged } = await render(2)
    await user.click(screen.getByRole('button', { name: 'Bind tag' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/tag UID or a tray UUID/)
    expect(onChanged).not.toHaveBeenCalled()
  })
})
