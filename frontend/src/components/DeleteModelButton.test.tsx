import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CataloguePage } from '../pages/CataloguePage'
import { api } from '../api/client'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { DELETE_RETRY_MS, DELETE_WAIT_LIMIT_MS, DeleteModelButton } from './DeleteModelButton'

function render(userEventOptions?: { advanceTimers: (ms: number) => unknown }, nativeLibrary = true) {
  return renderPage(
    <Routes>
      <Route path="/" element={<CataloguePage />} />
      <Route
        path="/m/:slug"
        element={<DeleteModelButton slug="name-keychain" name="Name Keychain" nativeLibrary={nativeLibrary} />}
      />
    </Routes>,
    { route: '/m/name-keychain', ...(userEventOptions ? { userEventOptions } : {}) },
  )
}

/** The 409 a delete gets while a render of the model is queued or running (#2387). */
const renderRunning = () =>
  HttpResponse.json(
    {
      type: 'about:blank',
      title: 'Conflict',
      status: 409,
      detail: "'name-keychain' has a render in progress; try again when it ends",
      render_in_progress: true,
    },
    { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
  )

const INBOX_COPIES = "Also delete the inbox copies in ScadBuddy's library"

describe('DeleteModelButton', () => {
  it('asks first, naming the model, and deletes nothing on cancel', async () => {
    const deletes: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'DELETE') deletes.push(new URL(request.url).pathname)
    })
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('alertdialog', { name: 'Delete Name Keychain?' })
    expect(dialog).toHaveTextContent('name-keychain')

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(deletes).toEqual([])
  })

  it('deletes on confirm and lands on a refreshed model list', async () => {
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Delete model' }))

    expect(await screen.findByRole('heading', { name: 'Gridfinity Bin' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Name Keychain' })).not.toBeInTheDocument()
  })

  it('offers the inbox copies in ScadBuddy\'s library, off by default', async () => {
    const searches: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'DELETE') searches.push(new URL(request.url).search)
    })
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('alertdialog', { name: 'Delete Name Keychain?' })
    const box = within(dialog).getByRole('checkbox', { name: INBOX_COPIES })
    expect(box).not.toBeChecked()
    expect(dialog).toHaveTextContent("Copies in Bambuddy's inbox are never deleted from here")
    await user.click(screen.getByRole('button', { name: 'Delete model' }))

    expect(await screen.findByRole('heading', { name: 'Gridfinity Bin' })).toBeInTheDocument()
    expect(searches).toEqual([''])
  })

  it("leaves the inbox copies out on Bambuddy's library (#5422)", async () => {
    const searches: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'DELETE') searches.push(new URL(request.url).search)
    })
    const { user } = render(undefined, false)

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('alertdialog', { name: 'Delete Name Keychain?' })
    expect(within(dialog).queryByRole('checkbox')).not.toBeInTheDocument()
    expect(dialog).not.toHaveTextContent("Bambuddy's inbox are never deleted")
    await user.click(screen.getByRole('button', { name: 'Delete model' }))

    expect(await screen.findByRole('heading', { name: 'Gridfinity Bin' })).toBeInTheDocument()
    expect(searches).toEqual([''])
  })

  it('sends delete_inbox_copies when ticked', async () => {
    const searches: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'DELETE') searches.push(new URL(request.url).search)
    })
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('checkbox', { name: INBOX_COPIES }))
    await user.click(screen.getByRole('button', { name: 'Delete model' }))

    expect(await screen.findByRole('heading', { name: 'Gridfinity Bin' })).toBeInTheDocument()
    expect(searches).toEqual(['?delete_inbox_copies=true'])
  })

  it('unticks the inbox copies on cancel', async () => {
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('checkbox', { name: INBOX_COPIES }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    expect(screen.getByRole('checkbox', { name: INBOX_COPIES })).not.toBeChecked()
  })

  it('shows the refusal while a print run of an output is open', async () => {
    server.use(
      http.delete('/api/v1/models/:slug', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Conflict',
            status: 409,
            detail: 'print run r-1 of an output of this model is still running',
            run_id: 'r-1',
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('checkbox', { name: INBOX_COPIES }))
    await user.click(screen.getByRole('button', { name: 'Delete model' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('print run r-1')
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })

  it('stays put and says why when the server refuses', async () => {
    server.use(
      http.delete('/api/v1/models/:slug', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Conflict',
            status: 409,
            detail: "'name-keychain' has a render in progress; try again when it ends",
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Delete model' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('render in progress')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Delete model' })).toBeEnabled(),
    )
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })

  it('names the duplicates that track the model and deletes only on "Delete anyway"', async () => {
    await api.duplicateModel('name-keychain', 'My Keychain')
    const deletes: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'DELETE') deletes.push(new URL(request.url).search)
    })
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Delete model' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('1 template is a duplicate of this one')
    expect(alert).toHaveTextContent('my-keychain')
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()

    await user.click(await screen.findByRole('button', { name: 'Delete anyway' }))

    expect(await screen.findByRole('heading', { name: 'My Keychain' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Name Keychain' })).not.toBeInTheDocument()
    expect(deletes).toEqual(['', '?force=true'])
  })

  it('forgets the duplicates on cancel, so the next delete asks again', async () => {
    await api.duplicateModel('name-keychain', 'My Keychain')
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Delete model' }))
    await screen.findByRole('button', { name: 'Delete anyway' })
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    expect(screen.getByRole('button', { name: 'Delete model' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows only the new error when the forced delete fails for another reason', async () => {
    await api.duplicateModel('name-keychain', 'My Keychain')
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Delete model' }))
    await screen.findByRole('button', { name: 'Delete anyway' })
    server.use(
      http.delete('/api/v1/models/:slug', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Conflict',
            status: 409,
            detail: "'name-keychain' has a render in progress; try again when it ends",
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    await user.click(screen.getByRole('button', { name: 'Delete anyway' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('render in progress')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.queryByText('my-keychain')).not.toBeInTheDocument()
  })

  describe('while a render of the model is in progress (#2387)', () => {
    afterEach(() => {
      vi.useRealTimers()
    })

    function fakeClock() {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      return render({ advanceTimers: vi.advanceTimersByTime })
    }

    it('says it is waiting, sends the delete again, and goes ahead once the render ends', async () => {
      let refusals = 2
      const deletes: string[] = []
      server.use(
        http.delete('/api/v1/models/:slug', ({ request }) => {
          deletes.push(request.method)
          if (refusals-- > 0) return renderRunning()
          return undefined
        }),
      )
      const { user } = fakeClock()

      await user.click(screen.getByRole('button', { name: 'Delete' }))
      await user.click(screen.getByRole('button', { name: 'Delete model' }))

      expect(await screen.findByRole('status')).toHaveTextContent('Waiting for it to finish, then deleting')
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      await vi.advanceTimersByTimeAsync(DELETE_RETRY_MS)
      await vi.advanceTimersByTimeAsync(DELETE_RETRY_MS)

      expect(await screen.findByRole('heading', { name: 'Gridfinity Bin' })).toBeInTheDocument()
      expect(screen.queryByRole('heading', { name: 'Name Keychain' })).not.toBeInTheDocument()
      expect(deletes).toHaveLength(3)
    })

    it('stops sending it on Cancel', async () => {
      let deletes = 0
      server.use(
        http.delete('/api/v1/models/:slug', () => {
          deletes += 1
          return renderRunning()
        }),
      )
      const { user } = fakeClock()

      await user.click(screen.getByRole('button', { name: 'Delete' }))
      await user.click(screen.getByRole('button', { name: 'Delete model' }))
      await screen.findByRole('status')
      await user.click(screen.getByRole('button', { name: 'Cancel' }))
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      await vi.advanceTimersByTimeAsync(DELETE_RETRY_MS * 5)

      expect(deletes).toBe(1)
    })

    it('offers no Cancel while a send is in flight, so a delete that lands still leaves the page', async () => {
      let release: () => void = () => {}
      let deletes = 0
      server.use(
        http.delete('/api/v1/models/:slug', async () => {
          deletes += 1
          if (deletes === 1) return renderRunning()
          await new Promise<void>((resolve) => (release = resolve))
          return undefined
        }),
      )
      const { user } = fakeClock()

      await user.click(screen.getByRole('button', { name: 'Delete' }))
      await user.click(screen.getByRole('button', { name: 'Delete model' }))
      await screen.findByRole('status')
      expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
      await vi.advanceTimersByTimeAsync(DELETE_RETRY_MS)
      await waitFor(() => expect(deletes).toBe(2))
      expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()

      release()
      expect(await screen.findByRole('heading', { name: 'Gridfinity Bin' })).toBeInTheDocument()
    })

    it('shows the refusal once it has waited its limit', async () => {
      server.use(http.delete('/api/v1/models/:slug', () => renderRunning()))
      const { user } = fakeClock()

      await user.click(screen.getByRole('button', { name: 'Delete' }))
      await user.click(screen.getByRole('button', { name: 'Delete model' }))
      await screen.findByRole('status')
      await vi.advanceTimersByTimeAsync(DELETE_WAIT_LIMIT_MS + DELETE_RETRY_MS)

      expect(await screen.findByRole('alert')).toHaveTextContent('render in progress')
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Delete model' })).toBeEnabled()
    })
  })
})
