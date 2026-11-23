import { useCallback, useMemo, useState, type FormEvent } from 'react'
import { aiPlugins, isBuiltIn, type PluginPackage } from '../../api/aiPlugins'
import { hostedPlugins, type HostedPlugin } from '../../api/hostedPlugins'
import { useAsync } from '../../lib/useAsync'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { Spinner } from '../ui/spinner'
import { LoadingLines, SettingBody, SettingCard } from './form'
import { HostedPluginEditor } from './HostedPluginEditor'
import { offerState } from './hostedDraft'
import { usePackagesChanged } from './packagesChanged'

/**
 * Settings → Tools & plugins → Hosted plugins (#6937, spec 2026-11-21 §6.2 and §6.3): Claude
 * plugins written here as a draft of files and published as numbered versions that never
 * change. The backend keeps the drafts and versions (`/api/v1/hosted-plugins`); the agent's
 * package install takes a version on an Offer, and Packages approves it as any package.
 */
export function HostedPluginsPanel() {
  const list = useAsync(() => hostedPlugins.list(), [])
  const packages = useAsync(() => aiPlugins.listPackages(), [])
  usePackagesChanged('hosted', () => packages.refresh())
  const [selected, setSelected] = useState<string | null>(null)
  /** The open editor's files with unsaved edits; a Close or another Edit asks first while any. */
  const [unsaved, setUnsaved] = useState<string[]>([])
  /** The plugin to open (null: none) once the person agrees to drop the unsaved edits. */
  const [pending, setPending] = useState<{ next: string | null } | null>(null)
  const onDirty = useCallback((paths: string[]) => setUnsaved(paths), [])
  const choose = (next: string | null) => {
    if (next === selected) return
    if (unsaved.length > 0) {
      setPending({ next })
      return
    }
    setSelected(next)
  }
  const plugins = useMemo(() => list.data ?? [], [list.data])
  const installed = useMemo(
    () => new Map((packages.data ?? []).filter((p): p is PluginPackage => !isBuiltIn(p)).map((p) => [p.name, p])),
    [packages.data],
  )
  /** The package of `name`: undefined when none has it, null when the packages could not be read. */
  const packageOf = (name: string) => (packages.data === undefined ? null : installed.get(name))
  const current = plugins.find((p) => p.name === selected) ?? null

  const replacePlugin = (next: HostedPlugin) =>
    list.setData(plugins.some((p) => p.name === next.name) ? plugins.map((p) => (p.name === next.name ? next : p)) : [...plugins, next])
  const replacePackage = (pkg: PluginPackage) => {
    const listed = packages.data ?? []
    packages.setData(listed.some((p) => !isBuiltIn(p) && p.name === pkg.name) ? listed.map((p) => (!isBuiltIn(p) && p.name === pkg.name ? pkg : p)) : [...listed, pkg])
  }

  return (
    <SettingCard
      title="Your plugins"
      headingId="hosted-plugins-list-heading"
      description="Write a Claude plugin here: skills, subagents and commands. Publish makes a numbered version that never changes; offer it to the assistant, then approve it in Packages."
    >
      <SettingBody>
        {list.loading && <LoadingLines label="Loading hosted plugins" lines={['h-10 w-full', 'h-10 w-full']} />}
        {list.error && (
          <p role="alert" className="text-xs text-warn">
            Hosted plugins are unavailable: {list.error.message}
          </p>
        )}
        {plugins.length > 0 && (
          <ul aria-label="Hosted plugins" className="divide-y rounded-lg border">
            {plugins.map((plugin) => (
              <PluginRow
                key={plugin.name}
                plugin={plugin}
                pkg={packageOf(plugin.name)}
                open={plugin.name === selected}
                onOpen={() => choose(plugin.name === selected ? null : plugin.name)}
              />
            ))}
          </ul>
        )}
        {!list.loading && !list.error && plugins.length === 0 && (
          <p className="text-xs text-muted-foreground">No hosted plugins yet. Create one below.</p>
        )}
        {current && (
          <div className="rounded-xl border bg-background p-4">
            <HostedPluginEditor
              key={current.name}
              plugin={current}
              pkg={packageOf(current.name)}
              onPlugin={replacePlugin}
              onPackage={replacePackage}
              onDeleted={(name) => {
                setSelected(null)
                setUnsaved([])
                list.setData(plugins.filter((p) => p.name !== name))
                packages.refresh()
              }}
              onClose={() => choose(null)}
              onDirty={onDirty}
            />
          </div>
        )}
        <CreateForm
          onCreated={(plugin) => {
            replacePlugin(plugin)
            choose(plugin.name)
          }}
        />
      </SettingBody>
      <AlertDialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Close without saving?</AlertDialogTitle>
            <AlertDialogDescription>
              {`Unsaved edits in ${unsaved.join(', ')} will be lost.`} Save each file first to keep its edits.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Stay</AlertDialogCancel>
            {/* A plain button: an AlertDialogAction would also close the dialog, which is Stay. */}
            <Button
              variant="destructive"
              onClick={() => {
                setSelected(pending?.next ?? null)
                setUnsaved([])
                setPending(null)
              }}
            >
              Close without saving
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </SettingCard>
  )
}

function PluginRow({ plugin, pkg, open, onOpen }: { plugin: HostedPlugin; pkg: PluginPackage | undefined | null; open: boolean; onOpen: () => void }) {
  const state = pkg === null ? null : offerState(plugin.latest_version, pkg)
  return (
    <li aria-label={`Hosted plugin ${plugin.name}`} className={cn('flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2', open && 'bg-muted/40')}>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium">{plugin.name}</p>
        {plugin.description && <p className="truncate text-xs text-muted-foreground">{plugin.description}</p>}
        {(plugin.category || plugin.tags.length > 0) && (
          <p className="mt-0.5 flex flex-wrap items-center gap-1 text-[11px] text-muted-foreground">
            {plugin.category && (
              <Badge variant="secondary" aria-label={`Category ${plugin.category}`}>
                {plugin.category}
              </Badge>
            )}
            {plugin.tags.length > 0 && <span aria-label={`Tags ${plugin.tags.join(', ')}`}>{plugin.tags.map((t) => `#${t}`).join(' ')}</span>}
          </p>
        )}
      </div>
      <span className="text-xs text-muted-foreground">
        {plugin.latest_version === null ? 'Draft only' : `Version ${plugin.latest_version}`}
      </span>
      {state?.kind === 'not-offered' && (
        <Badge variant="outline" className="border-warn/40 bg-warn/10 text-warn">
          Version {state.version} not offered
        </Badge>
      )}
      {(state?.kind === 'awaiting-approval' || state?.kind === 'repin-pending') && (
        <Badge variant="outline" className="border-warn/40 bg-warn/10 text-warn">
          Awaiting approval
        </Badge>
      )}
      {state?.kind === 'offered' && (
        <Badge variant="outline" className="border-ok/40 bg-ok/10 text-ok">
          {state.enabled ? 'Loaded' : 'Approved'}
        </Badge>
      )}
      <Button
        type="button"
        size="sm"
        variant={open ? 'secondary' : 'outline'}
        aria-expanded={open}
        aria-label={`${open ? 'Close' : 'Edit'} ${plugin.name}`}
        onClick={onOpen}
      >
        {open ? 'Close' : 'Edit'}
      </Button>
    </li>
  )
}

function CreateForm({ onCreated }: { onCreated: (plugin: HostedPlugin) => void }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onCreated(await hostedPlugins.create({ name: name.trim(), description }))
      setName('')
      setDescription('')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }
  return (
    <form aria-label="New hosted plugin" className="space-y-2 border-t pt-4" onSubmit={(e) => void submit(e)}>
      <p className="text-sm font-medium">New hosted plugin</p>
      <div className="grid gap-2 sm:grid-cols-[14rem_minmax(0,1fr)]">
        <div className="space-y-1">
          <Label htmlFor="hosted-create-name" className="text-xs">
            Name
          </Label>
          <Input id="hosted-create-name" value={name} placeholder="print-notes" onChange={(e) => setName(e.target.value)} aria-invalid={error !== null} />
          <p className="text-[11px] text-muted-foreground">2 to 32 lower-case letters, digits and hyphens. It never changes.</p>
        </div>
        <div className="space-y-1">
          <Label htmlFor="hosted-create-description" className="text-xs">
            Description
          </Label>
          <Textarea id="hosted-create-description" value={description} maxLength={1024} onChange={(e) => setDescription(e.target.value)} className="min-h-9" />
        </div>
      </div>
      <Button type="submit" size="sm" disabled={busy || name.trim() === ''} aria-busy={busy}>
        {busy && <Spinner />}
        Create plugin
      </Button>
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
    </form>
  )
}
