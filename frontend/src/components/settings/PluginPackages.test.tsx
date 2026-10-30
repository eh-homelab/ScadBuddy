import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import { aiPlugins } from '../../api/aiPlugins'
import { aiCapabilities } from '../../api/capabilities'
import { BUILT_IN_ANSWER, seedStoredPackage, GREETER_V1, GREETER_V2, MOVED_URL, RESERVED_PROBLEMS, SHELL, SHELL_PROBLEMS } from '../../mocks/aiPlugins'
import { GREETER_SKILL, LONG_CHANGELOG, PREVIEW_CHARS } from '../../mocks/features/pluginPackageFiles'
import { setPackageProblem } from '../../mocks/features/capabilities'
import { server } from '../../mocks/server'
import { accessName, choose, optionsOf } from '../../test/select'
import { renderPage } from '../../test/utils'
import { PluginPackagesPanel } from './PluginPackages'

// Radix Select scrolls its selected option into view; jsdom has no layout.
Element.prototype.scrollIntoView ??= () => {}

const NEW_HOOK = 'New since you last reviewed'
const HINT = 'Its hooks, skills, subagents and servers show here once the package is next loaded.'
const CHECKING = 'Checking whether "greeter" can load…'

async function itemOf(source: string, item: string) {
  const sources = await aiCapabilities.list()
  const find = (all: typeof sources): (typeof sources)[number] | undefined =>
    all.map((s) => (s.id === source ? s : find(s.children))).find((s) => s !== undefined)
  return find(sources)?.items.find((i) => i.id === item)
}

/** `card`'s package source, opened; once its first read's `checking` has settled (the page reads again). */
async function openSource(user: ReturnType<typeof renderPage>['user'], card: HTMLElement, name = 'greeter') {
  const source = await within(card).findByRole('region', { name: `Source ${name}` })
  await waitFor(() => expect(within(source).queryAllByText(CHECKING)).toEqual([]))
  await user.click(within(source).getByRole('button', { name }))
  return source
}

async function approve(user: ReturnType<typeof renderPage>['user'], card: HTMLElement) {
  await user.click(within(card).getByRole('button', { name: 'Approve…' }))
  const dialog = await screen.findByRole('dialog')
  await user.click(within(dialog).getByRole('checkbox'))
  await user.click(within(dialog).getByRole('button', { name: 'Approve this pin' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
}

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
    expect(isUserOnly(allow)).toBe(true)
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
    await user.click(await within(card).findByRole('switch', { name: 'Enabled (greeter)' }))
    expect(await within(card).findByText('Enabled', { selector: '[data-slot="badge"]' })).toBeInTheDocument()
    await user.click(within(card).getByRole('switch', { name: 'Enabled (greeter)' }))
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
    await user.click(await within(card).findByRole('switch', { name: 'Enabled (greeter)' }))
    expect(await within(card).findByText('Enabled', { selector: '[data-slot="badge"]' })).toBeInTheDocument()

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
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Remove package' }))
    expect(await screen.findByText('No plugin packages installed.')).toBeInTheDocument()
  })

  it("lists only installed packages; ScadBuddy's own is in Built-in", async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    expect(await screen.findByText('No plugin packages installed.')).toBeInTheDocument()
    expect(screen.queryByRole('listitem', { name: /scadbuddy/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: /^Source / })).not.toBeInTheDocument()
    await install(user, 'https://git.example/greeter.git')
    expect(await screen.findByRole('listitem', { name: 'Plugin package greeter' })).toBeInTheDocument()
    expect(screen.getAllByRole('listitem', { name: /^Plugin package / })).toHaveLength(1)
  })

  it('keeps a package stored under a built-in name marked not loaded, saying why, and removable', async () => {
    seedStoredPackage('scadbuddy')
    const { user } = renderPage(<PluginPackagesPanel />)
    const stored = await screen.findByRole('listitem', { name: 'Plugin package scadbuddy' })
    expect(within(stored).getByText('Not loaded')).toBeInTheDocument()
    expect(within(stored).getByText(/is built in, so this installed copy is never loaded/)).toBeInTheDocument()
    expect(within(stored).queryByLabelText('Re-pin to branch, tag or commit')).not.toBeInTheDocument()
    // Its row is on though a turn does not load it: the switch offers off.
    const source = await within(stored).findByRole('region', { name: 'Source scadbuddy' })
    expect(within(source).getByRole('note')).toHaveTextContent('plugin package scadbuddy was not loaded: "scadbuddy" is built in; remove the installed package')
    expect(within(source).getByRole('switch', { name: 'Enabled (scadbuddy)' })).toBeChecked()

    await user.click(within(stored).getByRole('button', { name: 'Remove' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Remove package' }))
    expect(await screen.findByText('No plugin packages installed.')).toBeInTheDocument()
  })

  it('shows a load problem on the card, and switching off works', async () => {
    seedStoredPackage('greeter')
    const problem = 'plugin package greeter is not loaded: skills/hello/SKILL.md changed since it was approved'
    setPackageProblem('greeter', problem)
    const { user } = renderPage(<PluginPackagesPanel />)
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    const source = await within(card).findByRole('region', { name: 'Source greeter' })
    expect(within(source).getByRole('note')).toHaveTextContent(problem)
    const toggle = within(source).getByRole('switch', { name: 'Enabled (greeter)' })
    expect(toggle).toBeChecked()
    await user.click(toggle)
    await waitFor(() => expect(within(source).getByRole('switch', { name: 'Enabled (greeter)' })).not.toBeChecked())
    expect(within(card).getByText('Approved, disabled')).toBeInTheDocument()
    expect((await aiPlugins.listPackages()).find((p) => p.name === 'greeter')?.enabled).toBe(false)
  })

  it('reads the package again while its load is being checked', async () => {
    seedStoredPackage('greeter')
    const { user } = renderPage(<PluginPackagesPanel />)
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    const source = await within(card).findByRole('region', { name: 'Source greeter' })
    await user.click(within(source).getByRole('button', { name: 'greeter' }))
    expect(within(source).getAllByText(CHECKING).length).toBeGreaterThan(0)
    // No reload: the page asks again shortly, and the second read has settled.
    await waitFor(() => expect(within(source).queryAllByText(CHECKING)).toEqual([]))
  })

  it("says an older review's items appear once the package is next loaded", async () => {
    seedStoredPackage('greeter', { review: { hooks: [{ event: 'Stop', type: 'prompt' }], described: undefined }, enabled: false })
    renderPage(<PluginPackagesPanel />)
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    expect(await within(card).findByText(HINT)).toBeInTheDocument()
  })

  it('enables an approved package with its source switch, not user-only', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await approve(user, card)
    expect(within(card).getByText('Approved, disabled')).toBeInTheDocument()
    // The one enable control: no button besides the switch.
    expect(within(card).queryByRole('button', { name: /^(Enable|Disable)$/ })).not.toBeInTheDocument()
    const toggle = await within(card).findByRole('switch', { name: 'Enabled (greeter)' })
    expect(isUserOnly(toggle)).toBe(false)
    expect(toggle).not.toBeChecked()
    await user.click(toggle)
    expect(await within(card).findByText('Enabled', { selector: '[data-slot="badge"]' })).toBeInTheDocument()
    expect((await aiPlugins.listPackages()).find((p) => p.name === 'greeter')?.enabled).toBe(true)
    await user.click(within(card).getByRole('switch', { name: 'Enabled (greeter)' }))
    expect(await within(card).findByText('Approved, disabled')).toBeInTheDocument()
  })

  it('offers no switch before approval', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    const source = await within(card).findByRole('region', { name: 'Source greeter' })
    expect(within(source).queryByRole('switch')).not.toBeInTheDocument()
  })

  it('turns a skill off after the confirmation', async () => {
    seedStoredPackage('greeter')
    const { user } = renderPage(<PluginPackagesPanel />)
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    const source = await openSource(user, card)
    // A hook, a skill and a subagent are on or off, nothing between (spec §4.1).
    for (const name of ['0f1e2d3c4b5a6978', 'hello', 'wave', 'helper']) {
      expect(await optionsOf(user, within(source).getByRole('combobox', { name: accessName(name) }))).toEqual(['off', 'on (default)'])
    }
    expect(within(source).getByText('Says hello to the user.')).toBeInTheDocument()
    const skill = within(source).getByRole('combobox', { name: accessName('hello') })
    expect(isUserOnly(skill)).toBe(false)
    await choose(user, skill, 'off')
    const dialog = await screen.findByRole('alertdialog', { name: 'Lower hello?' })
    expect(isUserOnly(within(dialog).getByRole('button', { name: 'Lower to off' }))).toBe(false)
    expect((await itemOf('package:greeter', 'skill:hello'))?.own_access).toBe('on')
    await user.click(within(dialog).getByRole('button', { name: 'Lower to off' }))
    await waitFor(() => expect(within(source).getByRole('combobox', { name: accessName('hello') })).toHaveTextContent('off'))
    expect((await itemOf('package:greeter', 'skill:hello'))?.access).toBe('off')
    expect(within(source).getByRole('button', { name: 'Reset hello' })).toBeInTheDocument()
  })

  it("sets a server tool's access after listing its tools", async () => {
    seedStoredPackage('greeter')
    const { user } = renderPage(<PluginPackagesPanel />)
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    const source = await openSource(user, card)
    const mem = within(source).getByRole('region', { name: 'Source mem' })
    const list = within(mem).getByRole('button', { name: 'List tools' })
    expect(isUserOnly(list)).toBe(false)
    await user.click(list)
    await user.click(within(mem).getByRole('button', { name: 'mem' }))
    const retain = await within(mem).findByRole('combobox', { name: accessName('retain') })
    expect(await optionsOf(user, retain)).toEqual(['off', 'read', 'write', 'outward (asks first) (default)'])
    await choose(user, retain, 'read')
    await user.click(within(await screen.findByRole('alertdialog', { name: 'Lower retain?' })).getByRole('button', { name: 'Lower to read' }))
    await waitFor(() => expect(within(mem).getByRole('combobox', { name: accessName('retain') })).toHaveTextContent('read'))
    expect((await itemOf('package:greeter/mcp:mem', 'retain'))?.access).toBe('read')
  })

  it('marks a hook new since you last reviewed', async () => {
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await within(card).findByRole('region', { name: 'Source greeter' })
    // The current review marks nothing.
    expect(within(card).queryByText(NEW_HOOK)).not.toBeInTheDocument()
    await user.type(within(card).getByLabelText('Re-pin to branch, tag or commit'), 'v2')
    await user.click(within(card).getByRole('button', { name: 'Fetch re-pin' }))
    await user.click(await within(card).findByRole('button', { name: 'Approve re-pin…' }))
    const review = within(screen.getByRole('dialog', { name: 'Approve the re-pin of greeter' })).getByLabelText('Review of greeter')
    const kept = within(review).getByText('Stop: prompt').closest('li')!
    const added = within(review).getByText('UserPromptSubmit: prompt').closest('li')!
    expect(within(kept).queryByText(NEW_HOOK)).not.toBeInTheDocument()
    expect(within(added).getByText(NEW_HOOK)).toBeInTheDocument()
    expect(within(review).getAllByText(NEW_HOOK)).toHaveLength(1)
  })

  it('marks no hook new when the current review predates hook ids', async () => {
    seedStoredPackage('greeter', { review: { hooks: [{ event: 'Stop', type: 'prompt' }], described: undefined } })
    const { user } = renderPage(<PluginPackagesPanel />)
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await within(card).findByRole('region', { name: 'Source greeter' })
    await user.type(within(card).getByLabelText('Re-pin to branch, tag or commit'), 'v2')
    await user.click(within(card).getByRole('button', { name: 'Fetch re-pin' }))
    await user.click(await within(card).findByRole('button', { name: 'Approve re-pin…' }))
    const review = within(screen.getByRole('dialog', { name: 'Approve the re-pin of greeter' })).getByLabelText('Review of greeter')
    expect(within(review).getByText('UserPromptSubmit: prompt')).toBeInTheDocument()
    expect(within(review).queryByText(NEW_HOOK)).not.toBeInTheDocument()
  })

  it('marks install, approve, re-pin and remove user-only and leaves every access control unmarked', async () => {
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
    await user.click(within(dialog).getByRole('checkbox'))
    await user.click(within(dialog).getByRole('button', { name: 'Approve this pin' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    await user.click(await within(card).findByRole('switch', { name: 'Enabled (greeter)' }))
    const source = await openSource(user, card)
    expect(isUserOnly(within(source).getByRole('switch', { name: 'Enabled (greeter)' }))).toBe(false)
    for (const select of within(source).getAllByRole('combobox')) expect(isUserOnly(select)).toBe(false)
    const mem = within(source).getByRole('region', { name: 'Source mem' })
    expect(isUserOnly(within(mem).getByRole('switch', { name: 'Enabled (mem)' }))).toBe(false)
    expect(isUserOnly(within(mem).getByRole('button', { name: 'List tools' }))).toBe(false)

    await user.type(within(card).getByLabelText('Re-pin to branch, tag or commit'), 'v2')
    await user.click(within(card).getByRole('button', { name: 'Fetch re-pin' }))
    const pending = await within(card).findByRole('generic', { name: 'Pending re-pin' })
    for (const name of ['Approve re-pin…', 'Discard re-pin']) {
      expect(isUserOnly(within(pending).getByRole('button', { name }))).toBe(true)
    }
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
    expect(within(viewer).getByRole('radio', { name: 'Raw' })).toHaveAttribute('aria-checked', 'true')

    await user.click(within(viewer).getByRole('radio', { name: 'Rendered' }))
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

  it('offers no "Show all" for a file too large to send whole (#2151)', async () => {
    const huge = 'y'.repeat(PREVIEW_CHARS)
    server.use(
      http.get('/api/v1/ai/plugin-packages/:name/file', () =>
        HttpResponse.json({
          path: 'CHANGELOG.txt',
          size: 2 * 1024 * 1024,
          binary: false,
          media_type: 'text/plain',
          truncated: true,
          too_large: true,
          content: huge,
        }),
      ),
    )
    const { user } = renderPage(<PluginPackagesPanel />)
    await screen.findByText('No plugin packages installed.')
    await install(user, 'https://git.example/greeter.git')
    const card = await screen.findByRole('listitem', { name: 'Plugin package greeter' })
    await openFiles(user, card)
    await user.click(await within(card).findByRole('button', { name: 'CHANGELOG.txt' }))
    const viewer = await within(card).findByRole('region', { name: 'greeter: CHANGELOG.txt' })
    expect(await within(viewer).findByTestId('file-content')).toHaveTextContent(huge)
    expect(within(viewer).getByText(/Too large to show whole\./)).toBeInTheDocument()
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
