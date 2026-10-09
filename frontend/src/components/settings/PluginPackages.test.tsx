import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import { BUILT_IN_ANSWER, seedStoredPackage, GREETER_V1, GREETER_V2, MOVED_URL, RESERVED_PROBLEMS, SHELL, SHELL_PROBLEMS } from '../../mocks/aiPlugins'
import { GREETER_SKILL, LONG_CHANGELOG, PREVIEW_CHARS } from '../../mocks/features/pluginPackageFiles'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { PluginPackagesPanel } from './PluginPackages'

async function openFiles(user: ReturnType<typeof renderPage>['user'], scope: HTMLElement) {
  await user.click(within(scope).getByText(/^Files to read \(/))
}

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

  it('lists the built-in plugins first; they switch on and off but are never removed or re-pinned', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    const own = await screen.findByRole('listitem', { name: 'Built-in plugin scadbuddy' })
    expect(within(own).getByText('Built in')).toBeInTheDocument()
    expect(within(own).getByText('Enabled')).toBeInTheDocument()
    expect(within(own).getByText('agent/plugins/scadbuddy')).toBeInTheDocument()
    expect(within(within(own).getByLabelText('Review of scadbuddy')).getByText('/scadbuddy:authoring')).toBeInTheDocument()
    expect(within(own).queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
    expect(within(own).queryByLabelText('Re-pin to branch, tag or commit')).not.toBeInTheDocument()

    const browser = screen.getByRole('listitem', { name: 'Built-in plugin playwright' })
    const enable = within(browser).getByRole('button', { name: 'Enable' })
    expect(isUserOnly(enable)).toBe(true)
    await user.click(enable)
    expect(await within(browser).findByText('Enabled')).toBeInTheDocument()
    expect(within(browser).getByRole('button', { name: 'Disable' })).toBeInTheDocument()
    // Built-ins are not installed packages.
    expect(screen.getByText('No plugin packages installed.')).toBeInTheDocument()
  })

  it('keeps a built-in and a package stored under its name apart', async () => {
    seedStoredPackage('playwright')
    const { user } = renderPage(<PluginPackagesPanel />)
    const builtIn = await screen.findByRole('listitem', { name: 'Built-in plugin playwright' })
    const stored = screen.getByRole('listitem', { name: 'Plugin package playwright' })
    await user.click(within(builtIn).getByRole('button', { name: 'Enable' }))
    expect(await within(builtIn).findByRole('button', { name: 'Disable' })).toBeInTheDocument()
    expect(screen.getByRole('listitem', { name: 'Plugin package playwright' })).toBe(stored)
    // The stored copy never loads: no switch, no re-pin, only Remove.
    expect(within(stored).getByText('Not loaded')).toBeInTheDocument()
    expect(within(stored).queryByRole('button', { name: /Enable|Disable/ })).not.toBeInTheDocument()
    expect(within(stored).queryByLabelText('Re-pin to branch, tag or commit')).not.toBeInTheDocument()

    await user.click(within(stored).getByRole('button', { name: 'Remove' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Remove package' }))
    await waitFor(() => expect(screen.queryByRole('listitem', { name: 'Plugin package playwright' })).not.toBeInTheDocument())
    expect(screen.getByRole('listitem', { name: 'Built-in plugin playwright' })).toBeInTheDocument()
  })

  it('answers an install of ScadBuddy\'s own plugin with a notice, not a refusal', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await user.click(screen.getByLabelText('Marketplace entry'))
    await user.type(screen.getByLabelText('Marketplace repository URL'), 'https://github.com/eh-homelab/ScadBuddy')
    await user.type(screen.getByLabelText('Plugin name in the marketplace'), 'scadbuddy')
    await user.click(screen.getByRole('button', { name: 'Fetch and review' }))
    expect(await screen.findByRole('status')).toHaveTextContent(BUILT_IN_ANSWER)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
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
    await install(user, 'https://git.example/reserved.git')
    const list = await screen.findByRole('list', { name: 'Why the package was refused' })
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual(RESERVED_PROBLEMS)
    expect(screen.queryByRole('listitem', { name: /Plugin package/ })).not.toBeInTheDocument()
  })

  it('approves a package the rules refuse only once loading it as it is is confirmed too', async () => {
    let approved: unknown
    server.events.on('request:start', ({ request }) => {
      if (request.url.endsWith('/approve')) void request.clone().json().then((b) => (approved = b))
    })
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/shell.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package shell' })
    expect(within(within(card).getByLabelText('Review of shell')).getByText('Bash')).toBeInTheDocument()
    const refused = within(card).getByRole('list', { name: 'What the rules refuse in shell' })
    expect(within(refused).getAllByRole('listitem').map((li) => li.textContent)).toEqual(SHELL_PROBLEMS)

    await user.click(within(card).getByRole('button', { name: 'Approve…' }))
    const dialog = await screen.findByRole('dialog')
    const approve = within(dialog).getByRole('button', { name: 'Approve this pin' })
    await user.click(within(dialog).getByRole('checkbox', { name: /I reviewed commit/ }))
    expect(approve).toBeDisabled()
    const allow = within(dialog).getByRole('checkbox', { name: /Load it as it is, despite the 2 refusals/ })
    expect(isUserOnly(allow.closest('label')!)).toBe(true)
    await user.click(allow)
    await user.click(approve)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(approved).toEqual({ commit_sha: SHELL.commit, content_hash: SHELL.hash, allow_refused: true })
    expect(within(card).getByText('Unvetted code allowed')).toBeInTheDocument()
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

  it('flags a re-pin that moved to another repository, and approving it leaves the package disabled', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await user.click(screen.getByLabelText('Marketplace entry'))
    await user.type(screen.getByLabelText('Marketplace repository URL'), 'https://git.example/market.git')
    await user.type(screen.getByLabelText('Plugin name in the marketplace'), 'greeter')
    await user.click(screen.getByRole('button', { name: 'Fetch and review' }))
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await user.click(within(card).getByRole('button', { name: 'Approve…' }))
    let dialog = screen.getByRole('dialog', { name: 'Approve greeter' })
    await user.click(within(dialog).getByRole('checkbox'))
    await user.click(within(dialog).getByRole('button', { name: 'Approve this pin' }))
    await user.click(await within(card).findByRole('button', { name: 'Enable' }))
    expect(await within(card).findByText('Enabled')).toBeInTheDocument()

    await user.type(within(card).getByLabelText('Re-pin to branch, tag or commit'), 'moved')
    await user.click(within(card).getByRole('button', { name: 'Fetch re-pin' }))
    const pending = await within(card).findByRole('generic', { name: 'Pending re-pin' })
    expect(within(pending).getByRole('note')).toHaveTextContent(
      `fetched from a different place: ${MOVED_URL}, not https://git.example/market.git · plugins/greeter`,
    )

    await user.click(within(pending).getByRole('button', { name: 'Approve re-pin…' }))
    dialog = screen.getByRole('dialog', { name: 'Approve the re-pin of greeter' })
    expect(within(dialog).getByRole('note')).toHaveTextContent('leaves the package disabled')
    await user.click(within(dialog).getByRole('checkbox'))
    await user.click(within(dialog).getByRole('button', { name: 'Approve this pin' }))
    expect(await within(card).findByText('Approved, disabled')).toBeInTheDocument()
    expect(within(card).getByText(GREETER_V2.commit)).toBeInTheDocument()
  })

  it('shows no move notice for a re-pin from the same repository', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await user.type(within(card).getByLabelText('Re-pin to branch, tag or commit'), 'v2')
    await user.click(within(card).getByRole('button', { name: 'Fetch re-pin' }))
    const pending = await within(card).findByRole('generic', { name: 'Pending re-pin' })
    expect(within(pending).queryByRole('note')).not.toBeInTheDocument()
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

  it("shows ScadBuddy's own skills and subagents on its card, with its version, without opening the review", async () => {
    renderPage(<PluginPackagesPanel />)
    const own = await screen.findByRole('listitem', { name: 'Built-in plugin scadbuddy' })
    expect(within(own).getByText('v0.1.10')).toBeInTheDocument()
    const parts = within(own).getByLabelText('What scadbuddy adds')
    expect(parts).toBeVisible()
    expect(parts).toHaveTextContent('Skills: /scadbuddy:authoring, /scadbuddy:customize, /scadbuddy:print')
    expect(parts).toHaveTextContent('Subagents: scadbuddy:model-author, scadbuddy:print-analyst')
    // The browser has neither, so says nothing.
    const browser = screen.getByRole('listitem', { name: 'Built-in plugin playwright' })
    expect(within(browser).queryByLabelText('What playwright adds')).not.toBeInTheDocument()
  })

  it('opens each file to read, with its size, and steps through every file', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await openFiles(user, card)
    const toRead = await within(card).findByRole('list', { name: 'Files to read in greeter' })
    const readme = within(toRead).getByRole('listitem', { name: 'README.md' })
    expect(await within(readme).findByText('22 B')).toBeInTheDocument()
    expect(within(card).getByText('Read 0 of 9 files')).toBeInTheDocument()

    await user.click(within(readme).getByRole('button', { name: 'README.md' }))
    let viewer = await within(card).findByRole('region', { name: 'greeter: README.md' })
    expect(await within(viewer).findByText(/A fixture\./)).toBeInTheDocument()
    expect(within(viewer).getByText(/22 B · text\/markdown/)).toBeInTheDocument()
    expect(within(card).getByText('Read 1 of 9 files')).toBeInTheDocument()

    await user.click(within(viewer).getByRole('button', { name: 'Next file' }))
    viewer = await within(card).findByRole('region', { name: 'greeter: agents/helper.md' })
    expect(await within(viewer).findByText(/Help\./)).toBeInTheDocument()
    await user.click(within(viewer).getByRole('button', { name: 'Previous file' }))
    expect(await within(card).findByRole('region', { name: 'greeter: README.md' })).toBeInTheDocument()
    expect(within(card).getByText('Read 2 of 9 files')).toBeInTheDocument()

    // Every file, not only the Markdown and JSON the review lists.
    const others = within(card).getByRole('list', { name: 'Other files in greeter' })
    expect(within(others).getAllByRole('button').map((b) => b.textContent)).toEqual(['CHANGELOG.txt', 'assets/icon.png'])
  })

  it('shows Markdown raw, highlighted, or rendered', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await openFiles(user, card)
    await user.click(await within(card).findByRole('button', { name: 'skills/hello/SKILL.md' }))
    const viewer = await within(card).findByRole('region', { name: 'greeter: skills/hello/SKILL.md' })
    const raw = await within(viewer).findByTestId('file-content')
    expect(raw.textContent).toBe(GREETER_SKILL)
    expect(within(raw).getByText('# Hello')).toHaveAttribute('data-token', 'heading')
    expect(within(viewer).getByRole('button', { name: 'Raw' })).toHaveAttribute('aria-pressed', 'true')

    await user.click(within(viewer).getByRole('button', { name: 'Rendered' }))
    expect(within(viewer).getByRole('heading', { name: 'Hello' })).toBeInTheDocument()
    expect(within(viewer).getByText('hello').tagName).toBe('STRONG')
  })

  it('shows a binary file as its size and type, and cuts a long file until asked for all of it', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await openFiles(user, card)
    await user.click(await within(card).findByRole('button', { name: 'assets/icon.png' }))
    let viewer = await within(card).findByRole('region', { name: 'greeter: assets/icon.png' })
    expect(await within(viewer).findByText('Binary file, not shown: 12 B · image/png')).toBeInTheDocument()
    expect(within(viewer).queryByTestId('file-content')).not.toBeInTheDocument()

    await user.click(within(card).getByRole('button', { name: 'CHANGELOG.txt' }))
    viewer = await within(card).findByRole('region', { name: 'greeter: CHANGELOG.txt' })
    const content = await within(viewer).findByTestId('file-content')
    expect(content.textContent).toHaveLength(PREVIEW_CHARS)
    expect(within(viewer).getByText(/Showing the first 66 kB of 91 kB\./)).toBeInTheDocument()
    await user.click(within(viewer).getByRole('button', { name: 'Show all' }))
    await waitFor(() => expect(within(viewer).getByTestId('file-content').textContent).toBe(LONG_CHANGELOG))
    expect(within(viewer).queryByRole('button', { name: 'Show all' })).not.toBeInTheDocument()
  })

  it("reads the re-pin's own files in its approval", async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await user.type(within(card).getByLabelText('Re-pin to branch, tag or commit'), 'v2')
    await user.click(within(card).getByRole('button', { name: 'Fetch re-pin' }))
    const pending = await within(card).findByRole('generic', { name: 'Pending re-pin' })
    await user.click(within(pending).getByRole('button', { name: 'Approve re-pin…' }))
    const dialog = screen.getByRole('dialog', { name: 'Approve the re-pin of greeter' })
    await openFiles(user, dialog)
    await user.click(await within(dialog).findByRole('button', { name: 'skills/bye/SKILL.md' }))
    const viewer = await within(dialog).findByRole('region', { name: 'greeter: skills/bye/SKILL.md' })
    expect(await within(viewer).findByText(/Say goodbye\./)).toBeInTheDocument()
  })

  it("reads a built-in plugin's files", async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    const own = await screen.findByRole('listitem', { name: 'Built-in plugin scadbuddy' })
    await user.click(within(own).getByText('Review'))
    await openFiles(user, own)
    await user.click(await within(own).findByRole('button', { name: 'skills/authoring/SKILL.md' }))
    const viewer = await within(own).findByRole('region', { name: 'scadbuddy: skills/authoring/SKILL.md' })
    expect(await within(viewer).findByText(/Writes OpenSCAD templates\./)).toBeInTheDocument()
  })

  it('says when a file cannot be read', async () => {
    server.use(
      http.get('/api/v1/ai/plugin-packages/:name/file', () =>
        HttpResponse.json({ detail: 'fetched commit abc, the pin is def' }, { status: 502 }),
      ),
    )
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await openFiles(user, card)
    await user.click(await within(card).findByRole('button', { name: 'README.md' }))
    const viewer = await within(card).findByRole('region', { name: 'greeter: README.md' })
    expect(await within(viewer).findByRole('alert')).toHaveTextContent('fetched commit abc, the pin is def')
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
