import { act, screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse, type JsonBodyType } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import { aiPlugins } from '../../api/aiPlugins'
import { hostedPlugins } from '../../api/hostedPlugins'
import { SEEDED_SKILL } from '../../mocks/features/hostedPlugins'
import { emitRealtime } from '../../mocks/realtime'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { PluginPackagesPanel } from './PluginPackages'
import { PACKAGES_HREF } from './HostedPluginEditor'
import { SettingsDirtyProvider, useDirtySections, useLeaveHolds } from '../../pages/settings/dirty'
import { HostedPluginsPanel } from './HostedPlugins'

// Monaco needs layout and workers jsdom lacks (SourceEditor.test); a textarea stands in.
vi.mock('./HostedFileEditor', () => ({
  default: ({
    path,
    initialValue,
    onChange,
    onSave,
  }: {
    path: string
    initialValue: string
    onChange: (text: string) => void
    onSave: () => void
  }) => (
    <textarea
      aria-label={`Edit ${path}`}
      defaultValue={initialValue}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.ctrlKey && e.key === 's') {
          e.preventDefault()
          onSave()
        }
      }}
    />
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

  it('creates a plugin, adds a skill, edits and saves it, publishes and offers the version, and lists it', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await createPlugin(user, 'wave-helper')
    expect(within(editor).getByText('Not published yet')).toBeInTheDocument()
    expect(within(editor).getByText('The draft is empty. Add a skill to start.')).toBeInTheDocument()

    const text = await addSkill(user, editor, 'wave')
    expect((text as HTMLTextAreaElement).value).toContain('name: wave')
    expect(within(editor).getByRole('button', { name: 'skills/wave/SKILL.md' })).toHaveAttribute('aria-current', 'true')

    await user.type(text, 'Wave twice.')
    const publish = within(editor).getByRole('button', { name: 'Publish version 1' })
    expect(publish).toBeDisabled()
    expect(within(editor).getByText(/Save your edits first/)).toBeInTheDocument()
    await user.click(within(editor).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(publish).toBeEnabled())
    expect(new TextDecoder().decode((await hostedPlugins.readFile('wave-helper', 'skills/wave/SKILL.md')).bytes)).toContain('Wave twice.')

    expect(isUserOnly(publish)).toBe(true)
    await user.click(publish)
    // Publish offers the new version at once (spec §6.3 step 5): installed, not approved.
    const publishing = within(editor).getByRole('region', { name: 'Publish' })
    expect(await within(publishing).findByText('Published version 1.')).toBeInTheDocument()
    expect(await within(publishing).findByText(/Offered version 1\. It is installed and waits for your approval\./)).toBeInTheDocument()
    expect(within(publishing).getByRole('link', { name: 'Review version 1 in Packages' })).toHaveAttribute('href', PACKAGES_HREF)
    const assistant = within(editor).getByRole('region', { name: 'Assistant' })
    expect(within(assistant).getByText('Version 1 is installed and waits for your approval.')).toBeInTheDocument()
    expect(within(editor).queryByRole('button', { name: 'Offer version 1' })).not.toBeInTheDocument()
    const pkg = (await aiPlugins.listPackages()).find((p) => p.name === 'wave-helper')
    expect(pkg).toMatchObject({ approved: false, enabled: false, version: 1, source: { kind: 'hosted', name: 'wave-helper' } })

    const versions = within(editor).getByRole('region', { name: 'Versions' })
    expect(within(versions).getByText('Version 1')).toBeInTheDocument()
    expect(within(versions).getByText('by user admin')).toHaveAttribute('title', 'user:https://id.example.com#admin')
    expect(within(editor).getByText('The draft is the same as version 1. Edit it to publish again.')).toBeInTheDocument()
    expect(within(editor).getByRole('button', { name: 'Publish version 2' })).toBeDisabled()
  })

  it('keeps Offer for a version whose offer failed after the publish', async () => {
    server.use(
      http.post('/api/v1/ai/plugin-packages', () => HttpResponse.json({ detail: 'the assistant is restarting' }, { status: 503 }), { once: true }),
    )
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await createPlugin(user, 'wave-helper')
    const text = await addSkill(user, editor, 'wave')
    await user.type(text, 'Wave.')
    await user.click(within(editor).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(within(editor).getByRole('button', { name: 'Publish version 1' })).toBeEnabled())
    await user.click(within(editor).getByRole('button', { name: 'Publish version 1' }))

    const publishing = within(editor).getByRole('region', { name: 'Publish' })
    expect(await within(publishing).findByText(/Version 1 is published, but the offer failed: .*the assistant is restarting/)).toBeInTheDocument()
    const assistant = within(editor).getByRole('region', { name: 'Assistant' })
    await user.click(within(assistant).getByRole('button', { name: 'Offer version 1' }))
    expect(await within(assistant).findByText('Offered version 1. It is installed and waits for your approval.')).toBeInTheDocument()
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
    expect(await within(editor).findByRole('button', { name: 'skills/print-notes/go.sh' })).toBeInTheDocument()
    expect(within(editor).queryByRole('button', { name: 'skills/print-notes/run.sh' })).not.toBeInTheDocument()

    await user.click(within(editor).getByRole('button', { name: 'Delete' }))
    await user.click(await screen.findByRole('button', { name: 'Delete file' }))
    await waitFor(() => expect(within(editor).queryByRole('button', { name: 'skills/print-notes/go.sh' })).not.toBeInTheDocument())
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
    const publishing = within(editor).getByRole('region', { name: 'Publish' })
    expect(await within(publishing).findByText('The draft cannot be published until you fix these:')).toBeInTheDocument()
    expect(within(publishing).getByText('skills/half/SKILL.md: the frontmatter needs a name and a description')).toBeInTheDocument()
    // The refusal is said once, beside Publish: not its headline again, and not in the draft.
    expect(within(editor).queryByText(/the draft cannot be published as it is/)).not.toBeInTheDocument()
    expect(within(editor).getAllByRole('alert')).toHaveLength(1)
    expect(within(editor).queryByRole('button', { name: 'Offer version 1' })).not.toBeInTheDocument()
  })

  it('publishes a new version of an installed plugin and offers it as a re-pin', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await openPlugin(user, 'print-notes')
    await user.click(await within(editor).findByRole('button', { name: 'Offer version 1' }))
    await within(editor).findByText('Version 1 is installed and waits for your approval.')

    await user.click(within(editor).getByRole('button', { name: 'skills/print-notes/SKILL.md' }))
    const text = await within(editor).findByLabelText('Edit skills/print-notes/SKILL.md')
    await user.type(text, 'Keep it short.')
    await user.click(within(editor).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(within(editor).getByRole('button', { name: 'Publish version 2' })).toBeEnabled())
    await user.click(within(editor).getByRole('button', { name: 'Publish version 2' }))
    const publishing = within(editor).getByRole('region', { name: 'Publish' })
    expect(await within(publishing).findByText('Published version 2.')).toBeInTheDocument()
    expect(await within(publishing).findByText(/Offered version 2 as a re-pin\. Version 1 loads until you approve it\./)).toBeInTheDocument()

    const assistant = within(editor).getByRole('region', { name: 'Assistant' })
    expect(within(assistant).getByText('Version 2 waits for your approval as a re-pin. Version 1 loads until then.')).toBeInTheDocument()
    expect(within(assistant).queryByRole('button', { name: 'Offer version 2' })).not.toBeInTheDocument()
    const pkg = (await aiPlugins.listPackages()).find((p) => p.name === 'print-notes')
    expect(pkg).toMatchObject({ version: 1, approved: false, pending: { version: 2 } })
  })

  it('follows a publish and re-pin made outside the page, in Packages and the editor (#7460)', async () => {
    await aiPlugins.installPackage({ kind: 'hosted', name: 'print-notes' })
    const { user } = renderPage(
      <>
        <PluginPackagesPanel />
        <HostedPluginsPanel />
      </>,
    )
    const card = await screen.findByRole('listitem', { name: 'Plugin package print-notes' })
    const editor = await openPlugin(user, 'print-notes')
    await within(editor).findByText('The draft is the same as version 1. Edit it to publish again.')
    expect(within(editor).getByRole('button', { name: 'Publish version 2' })).toBeDisabled()
    expect(within(card).queryByLabelText('Pending re-pin')).not.toBeInTheDocument()

    // What the assistant's `hosted_plugin_publish` does: a draft edit, a publish, a re-pin offer.
    const edited = new TextEncoder().encode(`${SEEDED_SKILL}Keep it short.\n`)
    await hostedPlugins.writeFile('print-notes', 'skills/print-notes/SKILL.md', edited, false)
    await hostedPlugins.publish('print-notes')
    await aiPlugins.installPackage({ kind: 'hosted', name: 'print-notes' })
    emitRealtime('plugin_packages.changed', ['plugin-packages'], { name: 'print-notes' })

    const pending = await within(card).findByLabelText('Pending re-pin')
    expect(pending).toHaveTextContent('Re-pin at version 2.')
    expect(within(pending).getByRole('button', { name: 'Approve re-pin…' })).toBeInTheDocument()
    expect(await within(editor).findByRole('button', { name: 'Publish version 3' })).toBeDisabled()
    expect(await within(editor).findByText('The draft is the same as version 2. Edit it to publish again.')).toBeInTheDocument()
    expect(within(within(editor).getByRole('region', { name: 'Versions' })).getByText('Version 2')).toBeInTheDocument()
  })

  it("keeps the newest draft diff when a topic read's answer lands after a save's (#7672)", async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await openPlugin(user, 'print-notes')
    await within(editor).findByText('The draft is the same as version 1. Edit it to publish again.')
    // Let the subscription's confirmation and any read it starts finish first.
    await act(() => new Promise((resolve) => setTimeout(resolve, 200)))

    // The topic's read sees the draft before the save and answers only after the save's read.
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => (release = resolve))
    let asked: () => void = () => {}
    const arrived = new Promise<void>((resolve) => (asked = resolve))
    server.use(
      http.get(
        '/api/v1/hosted-plugins/:name/draft-diff',
        async ({ request }) => {
          const seen = (await (await fetch(request.url)).json()) as JsonBodyType
          asked()
          await held
          return HttpResponse.json(seen)
        },
        { once: true },
      ),
      // The event's read also brings a version 2 published elsewhere.
      http.get(
        '/api/v1/hosted-plugins/:name/versions',
        async ({ request }) => {
          const seen = (await (await fetch(request.url)).json()) as { version: number }[]
          return HttpResponse.json([...seen, { ...seen.at(-1)!, version: 2 }] as JsonBodyType)
        },
        { once: true },
      ),
    )
    emitRealtime('plugin_packages.changed', ['plugin-packages'], { name: 'print-notes' })
    await arrived

    await user.click(within(editor).getByRole('button', { name: 'skills/print-notes/SKILL.md' }))
    await user.type(await within(editor).findByLabelText('Edit skills/print-notes/SKILL.md'), 'Keep it short.')
    await user.click(within(editor).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(within(editor).getByRole('button', { name: 'Publish version 2' })).toBeEnabled())

    release()
    await act(() => new Promise((resolve) => setTimeout(resolve, 200)))
    expect(within(editor).queryByText('The draft is the same as version 1. Edit it to publish again.')).not.toBeInTheDocument()
    // The save's draft read does not order the versions: the event's answer still lands.
    expect(within(within(editor).getByRole('region', { name: 'Versions' })).getByText('Version 2')).toBeInTheDocument()
  })

  it("reads nothing again for another plugin's change (#7672)", async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await openPlugin(user, 'print-notes')
    await within(editor).findByText('The draft is the same as version 1. Edit it to publish again.')
    await act(() => new Promise((resolve) => setTimeout(resolve, 200)))

    const reads: string[] = []
    const listener = ({ request }: { request: Request }) => {
      const path = new URL(request.url).pathname
      if (/\/hosted-plugins\/print-notes\/(draft-diff|versions)$/.test(path)) reads.push(path)
    }
    server.events.on('request:start', listener)
    try {
      emitRealtime('plugin_packages.changed', ['plugin-packages'], { name: 'greeter' })
      await act(() => new Promise((resolve) => setTimeout(resolve, 200)))
      expect(reads).toEqual([])

      emitRealtime('plugin_packages.changed', ['plugin-packages'], { name: 'print-notes' })
      await waitFor(() => expect(reads.length).toBeGreaterThan(0))
    } finally {
      server.events.removeListener('request:start', listener)
    }
  })

  it('shows the server refusal of a name, and edits the description, category and tags', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    await user.type(await screen.findByLabelText('Name'), 'Bad Name')
    await user.click(screen.getByRole('button', { name: 'Create plugin' }))
    expect(await screen.findByText(/is not 2-32 lower-case letters/)).toBeInTheDocument()

    const editor = await openPlugin(user, 'print-notes')
    const field = within(editor).getByLabelText('Description')
    await user.clear(field)
    await user.type(field, 'Short notes after each print')
    await user.click(within(editor).getByRole('button', { name: 'Save details' }))
    expect(await within(editor).findByText('Details saved.')).toBeInTheDocument()
    expect((await hostedPlugins.get('print-notes')).description).toBe('Short notes after each print')
    // Only the description changed: the next version still publishes it.
    expect(await within(editor).findByText('The description (plugin.json)')).toBeInTheDocument()
    expect(within(editor).getByRole('button', { name: 'Publish version 2' })).toBeEnabled()

    // A bad tag is refused before it is sent.
    await user.type(within(editor).getByLabelText('Tags'), 'Notes')
    await user.click(within(editor).getByRole('button', { name: 'Save details' }))
    expect(await within(editor).findByText('"Notes" is not a tag. A tag is 1 to 32 lower-case letters, digits and hyphens.')).toBeInTheDocument()
    await user.clear(within(editor).getByLabelText('Tags'))
    await user.type(within(editor).getByLabelText('Tags'), 'notes, prints notes')
    await user.type(within(editor).getByLabelText('Category'), 'printing')
    await user.click(within(editor).getByRole('button', { name: 'Save details' }))
    await within(editor).findByText('Details saved.')
    expect(await hostedPlugins.get('print-notes')).toMatchObject({ category: 'printing', tags: ['notes', 'prints'] })
    const row = screen.getByRole('listitem', { name: 'Hosted plugin print-notes' })
    expect(within(row).getByLabelText('Category printing')).toBeInTheDocument()
    expect(within(row).getByLabelText('Tags notes, prints')).toHaveTextContent('#notes #prints')

    // An empty category clears it.
    await user.clear(within(editor).getByLabelText('Category'))
    await user.click(within(editor).getByRole('button', { name: 'Save details' }))
    await waitFor(async () => expect((await hostedPlugins.get('print-notes')).category).toBeNull())
  })

  it('asks before Close or another plugin drops unsaved edits', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    await createPlugin(user, 'wave-helper')
    let editor = await openPlugin(user, 'print-notes')
    await user.click(within(editor).getByRole('button', { name: 'skills/print-notes/SKILL.md' }))
    await user.type(await within(editor).findByLabelText('Edit skills/print-notes/SKILL.md'), 'Keep it short.')

    // The editor's own Close asks; Stay keeps the edit.
    await user.click(within(editor).getByRole('button', { name: 'Close' }))
    let dialog = await screen.findByRole('alertdialog', { name: 'Close without saving?' })
    expect(within(dialog).getByText(/Unsaved edits in skills\/print-notes\/SKILL\.md will be lost\./)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Stay' }))
    expect((within(editor).getByLabelText('Edit skills/print-notes/SKILL.md') as HTMLTextAreaElement).value).toContain('Keep it short.')

    // Another plugin's Edit asks too; agreeing opens it and drops the edit.
    await user.click(screen.getByRole('button', { name: 'Edit wave-helper' }))
    dialog = await screen.findByRole('alertdialog', { name: 'Close without saving?' })
    await user.click(within(dialog).getByRole('button', { name: 'Close without saving' }))
    expect(await screen.findByRole('region', { name: 'Hosted plugin wave-helper' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Hosted plugin print-notes' })).not.toBeInTheDocument()

    // With nothing unsaved, Close closes at once.
    editor = screen.getByRole('region', { name: 'Hosted plugin wave-helper' })
    await user.click(within(editor).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Hosted plugin wave-helper' })).not.toBeInTheDocument())
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('holds a leave of Settings while a file is unsaved, never as a dirty section (#7192)', async () => {
    function Probe() {
      const holds = useLeaveHolds()
      const dirty = useDirtySections()
      return <output aria-label="leave holds">{`${holds.join('|')};${dirty.join(',')}`}</output>
    }
    const { user } = renderPage(
      <SettingsDirtyProvider>
        <HostedPluginsPanel />
        <Probe />
      </SettingsDirtyProvider>,
    )
    const unload = () => {
      const event = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(event)
      return event.defaultPrevented
    }
    const editor = await openPlugin(user, 'print-notes')
    expect(screen.getByRole('status', { name: 'leave holds' })).toHaveTextContent(/^;$/)
    expect(unload()).toBe(false)
    await user.click(within(editor).getByRole('button', { name: 'skills/print-notes/SKILL.md' }))
    await user.type(await within(editor).findByLabelText('Edit skills/print-notes/SKILL.md'), 'Keep it short.')
    // Held by its own words; the dirty sections (the reload's, the agent's) stay empty.
    expect(screen.getByRole('status', { name: 'leave holds' })).toHaveTextContent("print-notes's skills/print-notes/SKILL.md;")
    expect(unload()).toBe(true)
    // Closed without saving: the hold goes.
    await user.click(within(editor).getByRole('button', { name: 'Close' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Close without saving?' })
    await user.click(within(dialog).getByRole('button', { name: 'Close without saving' }))
    await waitFor(() => expect(screen.getByRole('status', { name: 'leave holds' })).toHaveTextContent(/^;$/))
    expect(unload()).toBe(false)
  })

  it('names up to three unsaved files in the leave hold, and counts more (#7260)', async () => {
    const extra = ['skills/print-notes/a.md', 'skills/print-notes/b.md', 'skills/print-notes/c.md']
    for (const path of extra) await hostedPlugins.writeFile('print-notes', path, new TextEncoder().encode('x'), false)
    function Probe() {
      return <output aria-label="leave holds">{useLeaveHolds().join('|')}</output>
    }
    const { user } = renderPage(
      <SettingsDirtyProvider>
        <HostedPluginsPanel />
        <Probe />
      </SettingsDirtyProvider>,
    )
    const editor = await openPlugin(user, 'print-notes')
    const paths = ['skills/print-notes/SKILL.md', ...extra]
    for (const path of paths) {
      await user.click(within(editor).getByRole('button', { name: path }))
      await user.type(await within(editor).findByLabelText(`Edit ${path}`), 'more')
    }
    expect(screen.getByRole('status', { name: 'leave holds' })).toHaveTextContent(/^print-notes's 4 files$/)
  })

  it('replaces a file that is not text with an upload, after a confirm, and keeps its executable bit', async () => {
    await hostedPlugins.writeFile('print-notes', 'skills/print-notes/blob.bin', new Uint8Array([0xff, 0xfe, 0x00]), true)
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await openPlugin(user, 'print-notes')
    await user.click(await within(editor).findByRole('button', { name: 'skills/print-notes/blob.bin' }))
    expect(await within(editor).findByText(/Upload a new copy at the same path to\s+replace it/)).toBeInTheDocument()

    await user.type(within(editor).getByLabelText('New file'), 'skills/print-notes/blob.bin')
    await user.upload(within(editor).getByLabelText('Upload a file into the draft'), new File(['now text'], 'x.txt'))
    expect(await screen.findByText('Replace skills/print-notes/blob.bin?')).toBeInTheDocument()
    // Nothing is written until the person confirms.
    expect((await hostedPlugins.readFile('print-notes', 'skills/print-notes/blob.bin')).bytes.length).toBe(3)
    await user.click(screen.getByRole('button', { name: 'Replace file' }))
    await waitFor(async () => {
      const read = await hostedPlugins.readFile('print-notes', 'skills/print-notes/blob.bin')
      expect(new TextDecoder().decode(read.bytes)).toBe('now text')
      expect(read.executable).toBe(true)
    })
  })

  it('shows an uploaded replacement of the open text file, and a later save sends it, not the old text', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await openPlugin(user, 'print-notes')
    await user.click(await within(editor).findByRole('button', { name: 'skills/print-notes/SKILL.md' }))
    const old = await within(editor).findByLabelText('Edit skills/print-notes/SKILL.md')
    const oldText = (old as HTMLTextAreaElement).value
    expect(oldText).not.toBe('')

    await user.type(within(editor).getByLabelText('New file'), 'skills/print-notes/SKILL.md')
    await user.upload(within(editor).getByLabelText('Upload a file into the draft'), new File(['uploaded body'], 'x.md'))
    await user.click(await screen.findByRole('button', { name: 'Replace file' }))
    await waitFor(() =>
      expect((within(editor).getByLabelText('Edit skills/print-notes/SKILL.md') as HTMLTextAreaElement).value).toBe('uploaded body'),
    )

    await user.type(within(editor).getByLabelText('Edit skills/print-notes/SKILL.md'), '!')
    await user.keyboard('{Control>}s{/Control}')
    await waitFor(async () => {
      const read = await hostedPlugins.readFile('print-notes', 'skills/print-notes/SKILL.md')
      expect(new TextDecoder().decode(read.bytes)).toBe('uploaded body!')
    })
  })

  it('sends one save for two quick Ctrl+S presses', async () => {
    let puts = 0
    server.use(
      http.put('/api/v1/hosted-plugins/:name/files/*', async () => {
        puts += 1
        await new Promise((resolve) => setTimeout(resolve, 150))
        return HttpResponse.json({ path: 'skills/print-notes/SKILL.md', size: 3, executable: false })
      }),
    )
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await openPlugin(user, 'print-notes')
    await user.click(await within(editor).findByRole('button', { name: 'skills/print-notes/SKILL.md' }))
    const text = await within(editor).findByLabelText('Edit skills/print-notes/SKILL.md')
    await user.type(text, 'x')
    await user.keyboard('{Control>}ss{/Control}')
    await waitFor(() => expect(within(editor).getByRole('button', { name: 'Save' })).toBeDisabled())
    expect(puts).toBe(1)
  })

  it('clears the offer result when a later publish changes the version', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await openPlugin(user, 'print-notes')
    await user.click(await within(editor).findByRole('button', { name: 'Offer version 1' }))
    const assistant = within(editor).getByRole('region', { name: 'Assistant' })
    expect(await within(assistant).findByText(/Offered version 1\./)).toBeInTheDocument()

    await user.click(within(editor).getByRole('button', { name: 'skills/print-notes/SKILL.md' }))
    await user.type(await within(editor).findByLabelText('Edit skills/print-notes/SKILL.md'), 'More.')
    await user.click(within(editor).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(within(editor).getByRole('button', { name: 'Publish version 2' })).toBeEnabled())
    await user.click(within(editor).getByRole('button', { name: 'Publish version 2' }))
    await within(editor).findByText('Published version 2.')
    expect(within(assistant).queryByText(/Offered version 1\./)).not.toBeInTheDocument()
  })

  it('clears the published notice when only the executable bit changes', async () => {
    const { user } = renderPage(<HostedPluginsPanel />)
    const editor = await openPlugin(user, 'print-notes')
    await user.click(await within(editor).findByRole('button', { name: 'skills/print-notes/SKILL.md' }))
    await user.type(await within(editor).findByLabelText('Edit skills/print-notes/SKILL.md'), 'More.')
    await user.click(within(editor).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(within(editor).getByRole('button', { name: 'Publish version 2' })).toBeEnabled())
    await user.click(within(editor).getByRole('button', { name: 'Publish version 2' }))
    await within(editor).findByText('Published version 2.')
    await user.click(within(editor).getByRole('checkbox', { name: 'Executable' }))
    await waitFor(() => expect(within(editor).queryByText('Published version 2.')).not.toBeInTheDocument())
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
