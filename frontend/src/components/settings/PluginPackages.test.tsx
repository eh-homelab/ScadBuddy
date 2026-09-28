import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import { GREETER_V1, GREETER_V2, SHELL_PROBLEMS } from '../../mocks/aiPlugins'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { PluginPackagesPanel } from './PluginPackages'

async function install(user: ReturnType<typeof renderPage>['user'], url: string, extra: Record<string, string> = {}) {
  await user.type(screen.getByLabelText('Repository URL'), url)
  if (extra.ref) await user.type(screen.getByLabelText('Branch, tag or commit'), extra.ref)
  await user.click(screen.getByRole('button', { name: 'Fetch and review' }))
}

describe('PluginPackagesPanel', () => {
  it('says when nothing is installed', async () => {
    renderPage(<PluginPackagesPanel />)
    expect(await screen.findByText('No plugin packages installed.')).toBeInTheDocument()
  })

  it('installs from git into an unapproved package and shows every part of the review', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')

    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    expect(within(card).getByText('Awaiting approval')).toBeInTheDocument()
    expect(within(card).getByText(GREETER_V1.commit)).toBeInTheDocument()
    expect(within(card).getByText(GREETER_V1.hash)).toBeInTheDocument()
    const review = within(card).getByLabelText('Review of greeter')
    expect(within(review).getByText('/greeter:hello')).toBeInTheDocument()
    expect(within(review).getByText('/greeter:wave')).toBeInTheDocument()
    expect(within(review).getByText('greeter:helper')).toBeInTheDocument()
    expect(within(review).getByText('Stop: prompt')).toBeInTheDocument()
    expect(within(review).getByText('mem (http) https://mcp.example/mcp/')).toBeInTheDocument()
    // Not approved, so it cannot be enabled yet.
    expect(within(card).queryByRole('button', { name: 'Enable' })).not.toBeInTheDocument()
  })

  it('installs a marketplace entry', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await user.click(screen.getByLabelText('Marketplace entry'))
    await user.type(screen.getByLabelText('Marketplace repository URL'), 'https://git.example/market.git')
    await user.type(screen.getByLabelText('Plugin name in the marketplace'), 'greeter')
    await user.click(screen.getByRole('button', { name: 'Fetch and review' }))
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    expect(within(card).getByText(/entry greeter/)).toBeInTheDocument()
  })

  it('lists each problem of a refused package', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/shell.git')
    const list = await screen.findByRole('list', { name: 'Why the package was refused' })
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual(SHELL_PROBLEMS)
    expect(screen.queryByRole('listitem', { name: /Plugin package/ })).not.toBeInTheDocument()
  })

  it('approves only after the exact commit and hash are shown and confirmed, then enables', async () => {
    let approved: unknown
    server.events.on('request:start', ({ request }) => {
      if (request.url.endsWith('/approve')) void request.clone().json().then((b) => (approved = b))
    })
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    await user.click(await screen.findByRole('button', { name: 'Approve…' }))

    const dialog = screen.getByRole('dialog', { name: 'Approve greeter' })
    expect(within(dialog).getByTestId('approve-commit')).toHaveTextContent(GREETER_V1.commit)
    expect(within(dialog).getByTestId('approve-hash')).toHaveTextContent(GREETER_V1.hash)
    const approve = within(dialog).getByRole('button', { name: 'Approve this pin' })
    expect(approve).toBeDisabled()
    await user.click(within(dialog).getByRole('checkbox'))
    await user.click(approve)

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(approved).toEqual({ commit_sha: GREETER_V1.commit, content_hash: GREETER_V1.hash })
    const card = screen.getByRole('listitem', { name: 'Plugin package greeter' })
    expect(within(card).getByText('Approved, disabled')).toBeInTheDocument()
    await user.click(within(card).getByRole('button', { name: 'Enable' }))
    expect(await within(card).findByText('Enabled')).toBeInTheDocument()
    await user.click(within(card).getByRole('button', { name: 'Disable' }))
    expect(await within(card).findByText('Approved, disabled')).toBeInTheDocument()
  })

  it('shows the server refusing an approval', async () => {
    server.use(
      http.post('/api/v1/ai/plugin-packages/:name/approve', () =>
        HttpResponse.json({ detail: 'the commit and content hash do not match the pin under review' }, { status: 409 }),
      ),
    )
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    await user.click(await screen.findByRole('button', { name: 'Approve…' }))
    const dialog = screen.getByRole('dialog')
    await user.click(within(dialog).getByRole('checkbox'))
    await user.click(within(dialog).getByRole('button', { name: 'Approve this pin' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('do not match')
  })

  it('re-pins with a diff, keeps the old pin until the re-pin is approved, and can discard it', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await user.type(within(card).getByLabelText('Re-pin to branch, tag or commit'), 'v2')
    await user.click(within(card).getByRole('button', { name: 'Fetch re-pin' }))

    const pending = await within(card).findByRole('generic', { name: 'Pending re-pin' })
    expect(pending).toHaveTextContent(GREETER_V2.commit)
    const diff = within(pending).getByRole('list', { name: 'Files changed by the re-pin' })
    expect(within(diff).getByText('skills/bye/SKILL.md').parentElement).toHaveTextContent('+')
    expect(within(diff).getByText('README.md').parentElement).toHaveTextContent('−')
    // The current pin is unchanged.
    expect(within(card).getByText(GREETER_V1.commit)).toBeInTheDocument()

    await user.click(within(pending).getByRole('button', { name: 'Approve re-pin…' }))
    const dialog = screen.getByRole('dialog', { name: 'Approve the re-pin of greeter' })
    expect(within(dialog).getByTestId('approve-commit')).toHaveTextContent(GREETER_V2.commit)
    expect(within(dialog).getByTestId('approve-hash')).toHaveTextContent(GREETER_V2.hash)
    await user.click(within(dialog).getByRole('checkbox'))
    await user.click(within(dialog).getByRole('button', { name: 'Approve this pin' }))
    await waitFor(() => expect(within(card).getByText(GREETER_V2.commit)).toBeInTheDocument())
    expect(within(card).queryByRole('generic', { name: 'Pending re-pin' })).not.toBeInTheDocument()

    await user.clear(within(card).getByLabelText('Re-pin to branch, tag or commit'))
    await user.type(within(card).getByLabelText('Re-pin to branch, tag or commit'), 'main')
    await user.click(within(card).getByRole('button', { name: 'Fetch re-pin' }))
    const again = await within(card).findByRole('generic', { name: 'Pending re-pin' })
    await user.click(within(again).getByRole('button', { name: 'Discard re-pin' }))
    await waitFor(() => expect(within(card).queryByRole('generic', { name: 'Pending re-pin' })).not.toBeInTheDocument())
  })

  it('removes a package after confirming', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await user.click(within(card).getByRole('button', { name: 'Remove' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove package' }))
    expect(await screen.findByText('No plugin packages installed.')).toBeInTheDocument()
  })

  it('marks every install, approve, enable, re-pin and remove control user-only', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    for (const label of ['Repository URL', 'Branch, tag or commit']) {
      expect(isUserOnly(screen.getByLabelText(label))).toBe(true)
    }
    expect(isUserOnly(screen.getByRole('button', { name: 'Fetch and review' }))).toBe(true)
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    for (const name of ['Approve…', 'Remove', 'Fetch re-pin']) {
      expect(isUserOnly(within(card).getByRole('button', { name }))).toBe(true)
    }
    await user.click(within(card).getByRole('button', { name: 'Approve…' }))
    const dialog = screen.getByRole('dialog')
    expect(isUserOnly(within(dialog).getByRole('button', { name: 'Approve this pin' }))).toBe(true)
    expect(isUserOnly(within(dialog).getByRole('checkbox'))).toBe(true)
  })

  it('says when the agent service is unavailable', async () => {
    server.use(
      http.get('/api/v1/ai/plugin-packages', () =>
        HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 }),
      ),
    )
    renderPage(<PluginPackagesPanel />)
    expect(await screen.findByRole('alert')).toHaveTextContent('AI features need the database')
  })
})
