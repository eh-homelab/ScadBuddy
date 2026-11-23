import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import { aiPlugins } from '../../api/aiPlugins'
import { hostedPlugins } from '../../api/hostedPlugins'
import { renderPage } from '../../test/utils'
import { PACKAGES_HREF } from './HostedPluginEditor'
import { HostedPluginsPanel } from './HostedPlugins'

// Monaco needs layout and workers jsdom lacks (SourceEditor.test); a textarea stands in.
vi.mock('./HostedFileEditor', () => ({
  default: ({ path, initialValue, onChange }: { path: string; initialValue: string; onChange: (text: string) => void }) => (
    <textarea aria-label={`Edit ${path}`} defaultValue={initialValue} onChange={(e) => onChange(e.target.value)} />
  ),
}))

type User = ReturnType<typeof renderPage>['user']

async function openPlugin(user: User, name: string) {
  await user.click(await screen.findByRole('button', { name: `Edit ${name}` }))
  return screen.findByRole('region', { name: `Hosted plugin ${name}` })
}

async function createPlugin(user: User, name: string, description = 'Things to try') {
  await user.type(await screen.findByLabelText('Name'), name)
  await user.type(screen.getByLabelText('Description', { selector: '#hosted-create-description' }), description)
  await user.click(screen.getByRole('button', { name: 'Create plugin' }))
  return screen.findByRole('region', { name: `Hosted plugin ${name}` })
}

async function addSkill(user: User, editor: HTMLElement, skill: string) {
  await user.type(within(editor).getByLabelText('New skill'), skill)
  await user.click(within(editor).getByRole('button', { name: 'Add' }))
  return within(editor).findByLabelText(`Edit skills/${skill}/SKILL.md`)
}

describe('HostedPluginsPanel', () => {
  it('lists a published plugin that was never offered and offers it as an unapproved install', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const row = await screen.findByRole('listitem', { name: 'Hosted plugin print-notes' })
    expect(await within(row).findByText('Version 1 not offered')).toBeInTheDocument()

    const editor = await openPlugin(user, 'print-notes')
    const assistant = within(editor).getByRole('region', { name: 'Assistant' })
    expect(await within(assistant).findByText(/Version 1 is not offered\. Offer it to install it/)).toBeInTheDocument()
    const offer = within(assistant).getByRole('button', { name: 'Offer version 1' })
    expect(isUserOnly(offer)).toBe(true)
    await user.click(offer)

    expect(await within(assistant).findByText('Offered version 1. It is installed and waits for your approval.')).toBeInTheDocument()
    expect(within(assistant).getByText('Version 1 is installed and waits for your approval.')).toBeInTheDocument()
    expect(within(assistant).getByRole('link', { name: 'Review and approve it in Packages' })).toHaveAttribute('href', PACKAGES_HREF)
    expect(within(row).getByText('Awaiting approval')).toBeInTheDocument()
    const pkg = (await aiPlugins.listPackages()).find((p) => p.name === 'print-notes')
    expect(pkg).toMatchObject({ approved: false, enabled: false, version: 1, source: { kind: 'hosted', name: 'print-notes' } })
  })

  it('creates a plugin, adds a skill, edits and saves it, publishes and lists the version', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await createPlugin(user, 'wave-helper')
    expect(within(editor).getByText('Not published yet')).toBeInTheDocument()
    expect(within(editor).getByText('The draft is empty. Add a skill to start.')).toBeInTheDocument()

    const text = await addSkill(user, editor, 'wave')
    expect((text as HTMLTextAreaElement).value).toContain('name: wave')
    expect(within(editor).getByRole('treeitem', { name: 'skills/wave/SKILL.md' })).toHaveAttribute('aria-selected', 'true')

    await user.type(text, 'Wave twice.')
    const publish = within(editor).getByRole('button', { name: 'Publish version 1' })
    expect(publish).toBeDisabled()
    expect(within(editor).getByText(/Save your edits first/)).toBeInTheDocument()
    await user.click(within(editor).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(publish).toBeEnabled())
    expect(new TextDecoder().decode((await hostedPlugins.readFile('wave-helper', 'skills/wave/SKILL.md')).bytes)).toContain('Wave twice.')

    expect(isUserOnly(publish)).toBe(true)
    await user.click(publish)
    expect(await within(editor).findByText('Published version 1.')).toBeInTheDocument()
    const versions = within(editor).getByRole('region', { name: 'Versions' })
    expect(within(versions).getByText('Version 1')).toBeInTheDocument()
    expect(within(editor).getByText('The draft is the same as version 1. Edit it to publish again.')).toBeInTheDocument()
    expect(within(editor).getByRole('button', { name: 'Publish version 2' })).toBeDisabled()
    expect(within(editor).getByRole('button', { name: 'Offer version 1' })).toBeInTheDocument()
  })

  it('says why a path or an executable bit is refused, the local check first and the server after', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await openPlugin(user, 'print-notes')
    await user.type(within(editor).getByLabelText('New file'), 'notes.md')
    await user.click(within(editor).getByRole('button', { name: 'Create empty' }))
    expect(await within(editor).findByText('A file must be under skills/, agents/ or commands/.')).toBeInTheDocument()

    await user.clear(within(editor).getByLabelText('New file'))
    await user.type(within(editor).getByLabelText('New file'), 'commands/wave.md')
    await user.click(within(editor).getByRole('button', { name: 'Create empty' }))
    await within(editor).findByLabelText('Edit commands/wave.md')
    expect(within(editor).getByRole('checkbox', { name: 'Executable' })).toBeDisabled()
    expect(within(editor).getByText('Only a file under skills/ can be executable.')).toBeInTheDocument()

    // The server's own refusal is shown as it says it: a path that clashes with a file.
    await user.type(within(editor).getByLabelText('New file'), 'commands/wave.md/more.md')
    await user.click(within(editor).getByRole('button', { name: 'Create empty' }))
    expect(await within(editor).findByText('commands/wave.md is a file, so it cannot hold commands/wave.md/more.md')).toBeInTheDocument()
  })

  it('renames and deletes a draft file, and marks a skill script executable', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await openPlugin(user, 'print-notes')
    await user.type(within(editor).getByLabelText('New file'), 'skills/print-notes/run.sh')
    await user.click(within(editor).getByRole('button', { name: 'Create empty' }))
    await within(editor).findByLabelText('Edit skills/print-notes/run.sh')
    await user.click(within(editor).getByRole('checkbox', { name: 'Executable' }))
    await waitFor(async () => expect((await hostedPlugins.readFile('print-notes', 'skills/print-notes/run.sh')).executable).toBe(true))

    await user.click(within(editor).getByRole('button', { name: 'Rename' }))
    const field = within(editor).getByLabelText('New path')
    await user.clear(field)
    await user.type(field, 'skills/print-notes/go.sh')
    await user.click(within(editor).getByRole('button', { name: 'Rename' }))
    expect(await within(editor).findByRole('treeitem', { name: 'skills/print-notes/go.sh' })).toBeInTheDocument()
    expect(within(editor).queryByRole('treeitem', { name: 'skills/print-notes/run.sh' })).not.toBeInTheDocument()

    await user.click(within(editor).getByRole('button', { name: 'Delete' }))
    await user.click(await screen.findByRole('button', { name: 'Delete file' }))
    await waitFor(() => expect(within(editor).queryByRole('treeitem', { name: 'skills/print-notes/go.sh' })).not.toBeInTheDocument())
    expect((await hostedPlugins.files('print-notes')).files.map((f) => f.path)).toEqual(['skills/print-notes/SKILL.md'])
  })

  it('lists what the draft check refuses when a publish fails', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await createPlugin(user, 'broken')
    const text = await addSkill(user, editor, 'half')
    await user.clear(text)
    await user.type(text, 'No frontmatter here.')
    await user.click(within(editor).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(within(editor).getByRole('button', { name: 'Publish version 1' })).toBeEnabled())
    await user.click(within(editor).getByRole('button', { name: 'Publish version 1' }))
    expect(await within(editor).findByText('The draft cannot be published until you fix these:')).toBeInTheDocument()
    expect(within(editor).getByText('skills/half/SKILL.md: the frontmatter needs a name and a description')).toBeInTheDocument()
  })

  it('offers a new version of an installed plugin as a re-pin', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await openPlugin(user, 'print-notes')
    await user.click(await within(editor).findByRole('button', { name: 'Offer version 1' }))
    await within(editor).findByText('Version 1 is installed and waits for your approval.')

    await user.click(within(editor).getByRole('treeitem', { name: 'skills/print-notes/SKILL.md' }))
    const text = await within(editor).findByLabelText('Edit skills/print-notes/SKILL.md')
    await user.type(text, 'Keep it short.')
    await user.click(within(editor).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(within(editor).getByRole('button', { name: 'Publish version 2' })).toBeEnabled())
    await user.click(within(editor).getByRole('button', { name: 'Publish version 2' }))
    await within(editor).findByText('Published version 2.')

    const assistant = within(editor).getByRole('region', { name: 'Assistant' })
    expect(within(assistant).getByText(/The package is pinned to version 1; an offer re-pins it/)).toBeInTheDocument()
    await user.click(within(assistant).getByRole('button', { name: 'Offer version 2' }))
    expect(await within(assistant).findByText('Offered version 2 as a re-pin. Version 1 loads until you approve it.')).toBeInTheDocument()
    expect(within(assistant).getByText('Version 2 waits for your approval as a re-pin. Version 1 loads until then.')).toBeInTheDocument()
  })

  it('shows the server refusal of a name, and edits the description', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    await user.type(await screen.findByLabelText('Name'), 'Bad Name')
    await user.click(screen.getByRole('button', { name: 'Create plugin' }))
    expect(await screen.findByText(/is not 2-32 lower-case letters/)).toBeInTheDocument()

    const editor = await openPlugin(user, 'print-notes')
    const field = within(editor).getByLabelText('Description')
    await user.clear(field)
    await user.type(field, 'Short notes after each print')
    await user.click(within(editor).getByRole('button', { name: 'Save description' }))
    expect(await within(editor).findByText('Description saved.')).toBeInTheDocument()
    expect((await hostedPlugins.get('print-notes')).description).toBe('Short notes after each print')
    // Only the description changed: the next version still publishes it.
    expect(await within(editor).findByText('The description (plugin.json)')).toBeInTheDocument()
    expect(within(editor).getByRole('button', { name: 'Publish version 2' })).toBeEnabled()
  })

  it('deletes a plugin and its package', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await openPlugin(user, 'print-notes')
    await user.click(await within(editor).findByRole('button', { name: 'Offer version 1' }))
    await within(editor).findByText('Version 1 is installed and waits for your approval.')
    await user.click(within(editor).getByRole('button', { name: 'Delete plugin' }))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Delete plugin' }))
    expect(await screen.findByText('No hosted plugins yet. Create one below.')).toBeInTheDocument()
    expect((await aiPlugins.listPackages()).some((p) => p.name === 'print-notes')).toBe(false)
  })
})
