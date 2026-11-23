import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link } from 'react-router'
import { FileIcon, FolderIcon } from 'lucide-react'
import { USER_ONLY } from '../../agent/dom'
import { aiPlugins, type PluginPackage } from '../../api/aiPlugins'
import {
  draftProblems,
  hostedPlugins,
  type HostedPlugin,
  type HostedPluginDraftDiff,
  type HostedPluginFiles,
  type HostedPluginVersion,
} from '../../api/hostedPlugins'
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
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { Spinner } from '../ui/spinner'
import {
  asText,
  canBeExecutable,
  fileTree,
  formatBytes,
  newSkill,
  offerState,
  pathProblem,
  skillNameProblem,
  type OfferState,
  type TreeNode,
} from './hostedDraft'
import { announcePackagesChanged } from './packagesChanged'

const HostedFileEditor = lazy(() => import('./HostedFileEditor'))

/** Where Packages lists a package for review (Settings → Tools & plugins → Packages). */
export const PACKAGES_HREF = '/settings/tools#packages'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** One open file: what the server holds, and the text being edited. */
interface OpenFile {
  path: string
  executable: boolean
  /** The saved text, or null for a file that is not UTF-8. */
  saved: string | null
  size: number
}

export interface HostedPluginEditorProps {
  plugin: HostedPlugin
  /** The package with the plugin's name; undefined when there is none, null while unknown (no assistant, or not read yet). */
  pkg: PluginPackage | undefined | null
  onPlugin: (plugin: HostedPlugin) => void
  onPackage: (pkg: PluginPackage) => void
  onDeleted: (name: string) => void
  onClose: () => void
}

/**
 * #6937 — one hosted plugin (spec 2026-11-21 §6.2, §6.3): its description, its draft as a file
 * tree with a Monaco editor, the draft against the latest version, Publish, the versions, and
 * the Offer that installs or re-pins the latest version as the assistant's package.
 *
 * Draft writes are not outward (they change nothing that loads), so the in-page agent may make
 * them. Publish, Offer and Delete are user-only (`USER_ONLY`): the assistant publishes through
 * its own tool, whose pending action a person approves.
 */
export function HostedPluginEditor({ plugin, pkg, onPlugin, onPackage, onDeleted, onClose }: HostedPluginEditorProps) {
  const name = plugin.name
  const [files, setFiles] = useState<HostedPluginFiles | null>(null)
  const [diff, setDiff] = useState<HostedPluginDraftDiff | null>(null)
  const [versions, setVersions] = useState<HostedPluginVersion[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [open, setOpen] = useState<OpenFile | null>(null)
  /** Unsaved text per path; a path with none is as saved. */
  const [buffers, setBuffers] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [published, setPublished] = useState<number | null>(null)
  const [problems, setProblems] = useState<string[]>([])

  const reloadDraft = useCallback(async () => {
    const [nextFiles, nextDiff] = await Promise.all([hostedPlugins.files(name), hostedPlugins.draftDiff(name)])
    setFiles(nextFiles)
    setDiff(nextDiff)
  }, [name])

  useEffect(() => {
    let live = true
    Promise.all([hostedPlugins.files(name), hostedPlugins.draftDiff(name), hostedPlugins.versions(name)]).then(
      ([f, d, v]) => {
        if (!live) return
        setFiles(f)
        setDiff(d)
        setVersions(v)
      },
      (caught: unknown) => live && setLoadError(message(caught)),
    )
    return () => {
      live = false
    }
  }, [name])

  async function act(what: string, run: () => Promise<void>) {
    setBusy(what)
    setError(null)
    try {
      await run()
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(null)
    }
  }

  async function openFile(path: string) {
    const info = files?.files.find((f) => f.path === path)
    await act(`open:${path}`, async () => {
      const read = await hostedPlugins.readFile(name, path)
      setOpen({ path, executable: read.executable, saved: asText(read.bytes), size: info?.size ?? read.bytes.length })
    })
  }

  const dirty = Object.keys(buffers)
  const openText = open ? (buffers[open.path] ?? open.saved) : null

  function edit(path: string, text: string) {
    setBuffers((current) => {
      const next = { ...current }
      if (open?.path === path && text === open.saved) delete next[path]
      else next[path] = text
      return next
    })
  }

  async function save(target: OpenFile, executable = target.executable) {
    const text = buffers[target.path] ?? target.saved
    if (text === null) return
    const bytes = new TextEncoder().encode(text)
    if (files && bytes.length > files.max_file_bytes) {
      setError(`${target.path} is ${formatBytes(bytes.length)}; a file is at most ${formatBytes(files.max_file_bytes)}.`)
      return
    }
    await act('save', async () => {
      const written = await hostedPlugins.writeFile(name, target.path, bytes, executable)
      setOpen({ path: target.path, executable: written.executable, saved: text, size: written.size })
      setBuffers(({ [target.path]: _saved, ...rest }) => rest)
      setPublished(null)
      await reloadDraft()
    })
  }

  async function setExecutable(executable: boolean) {
    if (!open || open.saved === null) return
    await act('executable', async () => {
      const written = await hostedPlugins.writeFile(name, open.path, new TextEncoder().encode(open.saved ?? ''), executable)
      setOpen({ ...open, executable: written.executable })
      await reloadDraft()
    })
  }

  async function create(path: string, content: BlobPart, executable = false): Promise<boolean> {
    const local = pathProblem(path, executable)
    if (local) {
      setError(local)
      return false
    }
    if (files?.files.some((f) => f.path === path)) {
      setError(`The draft already has ${path}. Open it from the tree.`)
      return false
    }
    let made = false
    await act('create', async () => {
      await hostedPlugins.writeFile(name, path, content, executable)
      await reloadDraft()
      setPublished(null)
      made = true
    })
    if (made) await openFile(path)
    return made
  }

  async function rename(to: string) {
    if (!open) return
    const local = pathProblem(to, open.executable)
    if (local) {
      setError(local)
      return
    }
    await act('rename', async () => {
      const moved = await hostedPlugins.moveFile(name, open.path, to)
      setBuffers(({ [open.path]: text, ...rest }) => (text === undefined ? rest : { ...rest, [moved.path]: text }))
      setOpen({ ...open, path: moved.path })
      await reloadDraft()
    })
  }

  async function remove() {
    if (!open) return
    await act('delete-file', async () => {
      await hostedPlugins.deleteFile(name, open.path)
      setBuffers(({ [open.path]: _gone, ...rest }) => rest)
      setOpen(null)
      await reloadDraft()
    })
  }

  async function publish() {
    setProblems([])
    await act('publish', async () => {
      try {
        const version = await hostedPlugins.publish(name)
        setPublished(version.version)
        setVersions((current) => [version, ...(current ?? [])])
        onPlugin({ ...plugin, latest_version: version.version })
        await reloadDraft()
      } catch (caught) {
        setProblems(draftProblems(caught))
        throw caught
      }
    })
  }

  return (
    <div className="space-y-5" aria-label={`Hosted plugin ${name}`} role="region">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h5 className="font-heading text-base font-medium">{name}</h5>
        <span className="text-xs text-muted-foreground">
          {plugin.latest_version === null ? 'Not published yet' : `Latest version ${plugin.latest_version}`}
        </span>
        <Button type="button" variant="ghost" size="sm" className="ml-auto" onClick={onClose}>
          Close
        </Button>
      </header>

      <DescriptionForm plugin={plugin} onSaved={(next) => {
        onPlugin(next)
        void reloadDraft()
      }} />

      {loadError && (
        <p role="alert" className="text-xs text-warn">
          The draft could not be read: {loadError}
        </p>
      )}

      <section aria-label="Draft files" className="space-y-2">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h6 className="text-sm font-medium">Draft</h6>
          {files && <CapsLine files={files} />}
        </div>
        <div className="grid gap-3 md:grid-cols-[15rem_minmax(0,1fr)]">
          <div className="space-y-3 rounded-lg border bg-muted/30 p-2">
            {!files && !loadError && <Skeleton className="h-24 w-full" />}
            {files && files.files.length === 0 && (
              <p className="px-1 text-xs text-muted-foreground">The draft is empty. Add a skill to start.</p>
            )}
            {files && files.files.length > 0 && (
              <Tree
                nodes={fileTree(files.files.map((f) => f.path))}
                executable={new Set(files.files.filter((f) => f.executable).map((f) => f.path))}
                dirty={new Set(dirty)}
                selected={open?.path ?? null}
                onOpen={(path) => void openFile(path)}
              />
            )}
            <AddFiles busy={busy === 'create'} onCreate={create} />
          </div>
          <div className="min-w-0">
            {open ? (
              <FilePane
                key={open.path}
                plugin={name}
                file={open}
                text={openText}
                dirty={buffers[open.path] !== undefined}
                busy={busy}
                onChange={(text) => edit(open.path, text)}
                onSave={() => void save(open)}
                onExecutable={(on) => void setExecutable(on)}
                onRename={(to) => void rename(to)}
                onDelete={() => void remove()}
              />
            ) : (
              <div className="flex h-full min-h-32 items-center justify-center rounded-lg border border-dashed p-6 text-center text-xs text-muted-foreground">
                {busy?.startsWith('open:') ? <Spinner /> : 'Choose a file in the tree to edit it.'}
              </div>
            )}
          </div>
        </div>
        {error && (
          <p role="alert" className="text-xs text-warn">
            {error}
          </p>
        )}
      </section>

      <section aria-label="Publish" className="space-y-2 rounded-lg border p-3">
        <h6 className="text-sm font-medium">Publish</h6>
        <DraftChanges diff={diff} />
        {dirty.length > 0 && (
          <p className="text-xs text-warn">
            Save your edits first: Publish sends the saved draft. Unsaved: {dirty.join(', ')}.
          </p>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            size="sm"
            disabled={busy !== null || dirty.length > 0 || !diff || nothingToPublish(diff)}
            aria-busy={busy === 'publish'}
            onClick={() => void publish()}
            {...USER_ONLY}
          >
            {busy === 'publish' && <Spinner />}
            Publish version {(plugin.latest_version ?? 0) + 1}
          </Button>
          {published !== null && (
            <p role="status" className="text-xs text-ok">
              Published version {published}.
            </p>
          )}
        </div>
        {problems.length > 0 && (
          <div role="alert" className="text-xs text-warn">
            <p>The draft cannot be published until you fix these:</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-5">
              {problems.map((p) => (
                <li key={p} className="break-words">
                  {p}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <OfferPanel plugin={plugin} pkg={pkg} onPackage={onPackage} />

      <Versions versions={versions} />

      <DeletePlugin name={name} onDeleted={onDeleted} />
    </div>
  )
}

const nothingToPublish = (diff: HostedPluginDraftDiff) =>
  diff.base_version !== null && !diff.added.length && !diff.changed.length && !diff.removed.length && !diff.manifest_changed

function CapsLine({ files }: { files: HostedPluginFiles }) {
  const share = files.max_total_bytes ? Math.min(1, files.total_bytes / files.max_total_bytes) : 0
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <span className="sb-num">
        {files.files.length} of {files.max_files} files, {formatBytes(files.total_bytes)} of {formatBytes(files.max_total_bytes)}
      </span>
      <span aria-hidden="true" className="h-1 w-16 overflow-hidden rounded-full bg-muted">
        <span className={cn('block h-full', share > 0.9 ? 'bg-warn' : 'bg-primary/60')} style={{ width: `${share * 100}%` }} />
      </span>
    </div>
  )
}

function Tree({
  nodes,
  executable,
  dirty,
  selected,
  onOpen,
  depth = 0,
}: {
  nodes: TreeNode[]
  executable: ReadonlySet<string>
  dirty: ReadonlySet<string>
  selected: string | null
  onOpen: (path: string) => void
  depth?: number
}) {
  return (
    <ul role={depth === 0 ? 'tree' : 'group'} aria-label={depth === 0 ? 'Draft files' : undefined} className="text-xs">
      {nodes.map((node) =>
        node.kind === 'folder' ? (
          <li key={node.path} role="treeitem" aria-expanded="true" aria-label={`${node.name}/`}>
            <div className="flex items-center gap-1 py-0.5 text-muted-foreground" style={{ paddingLeft: `${depth * 0.75}rem` }}>
              <FolderIcon aria-hidden="true" className="size-3.5" />
              {node.name}/
            </div>
            <Tree nodes={node.children} executable={executable} dirty={dirty} selected={selected} onOpen={onOpen} depth={depth + 1} />
          </li>
        ) : (
          <li key={node.path} role="none">
            <button
              type="button"
              role="treeitem"
              aria-selected={selected === node.path}
              aria-label={node.path}
              onClick={() => onOpen(node.path)}
              className={cn(
                'flex min-h-6 w-full min-w-0 items-center gap-1 rounded px-1 py-0.5 text-left hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none',
                selected === node.path && 'bg-primary/10 font-medium text-foreground',
              )}
              style={{ paddingLeft: `${depth * 0.75 + 0.25}rem` }}
            >
              <FileIcon aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
              <span className="truncate">{node.name}</span>
              {executable.has(node.path) && <span className="sb-num text-[10px] text-muted-foreground">exec</span>}
              {dirty.has(node.path) && <span title="Unsaved" aria-label="unsaved" className="ml-auto size-1.5 shrink-0 rounded-full bg-warn" />}
            </button>
          </li>
        ),
      )}
    </ul>
  )
}

function AddFiles({ busy, onCreate }: { busy: boolean; onCreate: (path: string, content: BlobPart, executable?: boolean) => Promise<boolean> }) {
  const [skill, setSkill] = useState('')
  const [skillError, setSkillError] = useState<string | null>(null)
  const [path, setPath] = useState('')
  const upload = useRef<HTMLInputElement>(null)

  async function addSkill(event: FormEvent) {
    event.preventDefault()
    const problem = skillNameProblem(skill)
    setSkillError(problem)
    if (problem) return
    if (await onCreate(`skills/${skill}/SKILL.md`, newSkill(skill))) setSkill('')
  }

  async function addFile(event: FormEvent) {
    event.preventDefault()
    if (await onCreate(path, '')) setPath('')
  }

  async function uploaded(file: File | undefined) {
    if (!file) return
    const target = path === '' || path.endsWith('/') ? `${path}${file.name}` : path
    if (await onCreate(target, file)) setPath('')
    if (upload.current) upload.current.value = ''
  }

  return (
    <div className="space-y-3 border-t pt-3">
      <form className="space-y-1" onSubmit={(e) => void addSkill(e)}>
        <Label htmlFor="hosted-new-skill" className="text-xs">
          New skill
        </Label>
        <div className="flex gap-1.5">
          <Input
            id="hosted-new-skill"
            value={skill}
            placeholder="skill-name"
            onChange={(e) => setSkill(e.target.value)}
            aria-invalid={skillError !== null}
            className="h-7 text-xs"
          />
          <Button type="submit" size="sm" variant="outline" disabled={busy || skill === ''}>
            Add
          </Button>
        </div>
        <p className="text-[11px] text-muted-foreground">Writes skills/&lt;name&gt;/SKILL.md with its frontmatter.</p>
        {skillError && <p className="text-[11px] text-warn">{skillError}</p>}
      </form>
      <form className="space-y-1" onSubmit={(e) => void addFile(e)}>
        <Label htmlFor="hosted-new-file" className="text-xs">
          New file
        </Label>
        <Input
          id="hosted-new-file"
          value={path}
          placeholder="commands/wave.md"
          onChange={(e) => setPath(e.target.value)}
          className="h-7 text-xs"
        />
        <div className="flex flex-wrap gap-1.5">
          <Button type="submit" size="sm" variant="outline" disabled={busy || path === '' || path.endsWith('/')}>
            Create empty
          </Button>
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => upload.current?.click()}>
            Upload…
          </Button>
          <input
            ref={upload}
            type="file"
            aria-label="Upload a file into the draft"
            className="sr-only"
            tabIndex={-1}
            onChange={(e) => void uploaded(e.target.files?.[0])}
          />
        </div>
        <p className="text-[11px] text-muted-foreground">Under skills/, agents/ or commands/. A path ending in / takes the uploaded file&rsquo;s name.</p>
      </form>
    </div>
  )
}

function FilePane({
  plugin,
  file,
  text,
  dirty,
  busy,
  onChange,
  onSave,
  onExecutable,
  onRename,
  onDelete,
}: {
  plugin: string
  file: OpenFile
  text: string | null
  dirty: boolean
  busy: string | null
  onChange: (text: string) => void
  onSave: () => void
  onExecutable: (on: boolean) => void
  onRename: (to: string) => void
  onDelete: () => void
}) {
  const [renaming, setRenaming] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const executableAllowed = canBeExecutable(file.path)
  return (
    <div className="space-y-2">
      {renaming === null ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="sb-num min-w-0 truncate text-xs font-medium" title={file.path}>
            {file.path}
          </span>
          {dirty && <Badge variant="outline" className="border-warn/40 bg-warn/10 text-warn">Unsaved</Badge>}
          <div className="ml-auto flex flex-wrap gap-1.5">
            <Button type="button" size="sm" variant="ghost" onClick={() => setRenaming(file.path)} disabled={busy !== null}>
              Rename
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setConfirmDelete(true)} disabled={busy !== null}>
              Delete
            </Button>
            <Button type="button" size="sm" onClick={onSave} disabled={!dirty || busy !== null} aria-busy={busy === 'save'}>
              {busy === 'save' && <Spinner />}
              Save
            </Button>
          </div>
        </div>
      ) : (
        <form
          className="flex flex-wrap items-center gap-1.5"
          onSubmit={(e) => {
            e.preventDefault()
            onRename(renaming)
            setRenaming(null)
          }}
        >
          <Label htmlFor="hosted-rename" className="sr-only">
            New path
          </Label>
          <Input id="hosted-rename" value={renaming} onChange={(e) => setRenaming(e.target.value)} className="h-7 min-w-48 flex-1 text-xs" autoFocus />
          <Button type="submit" size="sm" disabled={renaming === file.path || renaming === ''}>
            Rename
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setRenaming(null)}>
            Cancel
          </Button>
        </form>
      )}
      <div className="flex items-center gap-2">
        <Checkbox
          id="hosted-executable"
          checked={file.executable}
          disabled={!executableAllowed || file.saved === null || busy !== null}
          onCheckedChange={(on) => onExecutable(on === true)}
        />
        <Label htmlFor="hosted-executable" className="text-xs font-normal">
          Executable
        </Label>
        <span className="text-[11px] text-muted-foreground">
          {executableAllowed ? 'A script the skill runs.' : 'Only a file under skills/ can be executable.'}
        </span>
      </div>
      {text === null ? (
        <p className="rounded-lg border border-dashed p-6 text-center text-xs text-muted-foreground">
          This file is not text ({formatBytes(file.size)}), so it is not edited here. Upload a new copy at the same path, or
          delete it.
        </p>
      ) : (
        <Suspense fallback={<Skeleton role="status" aria-label="Loading the editor" className="h-[26rem] w-full" />}>
          <HostedFileEditor plugin={plugin} path={file.path} initialValue={text} onChange={onChange} onSave={onSave} />
        </Suspense>
      )}
      <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {file.path}?</AlertDialogTitle>
            <AlertDialogDescription>
              It leaves the draft. Published versions keep their copy.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep it</AlertDialogCancel>
            <Button
              type="button"
              variant="destructive"
              onClick={() => {
                setConfirmDelete(false)
                onDelete()
              }}
            >
              Delete file
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

function DescriptionForm({ plugin, onSaved }: { plugin: HostedPlugin; onSaved: (plugin: HostedPlugin) => void }) {
  const [text, setText] = useState(plugin.description)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      onSaved(await hostedPlugins.update(plugin.name, { description: text }))
      setSaved(true)
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }
  return (
    <form className="space-y-1" onSubmit={(e) => void submit(e)}>
      <Label htmlFor="hosted-description" className="text-sm">
        Description
      </Label>
      <Textarea
        id="hosted-description"
        value={text}
        maxLength={1024}
        onChange={(e) => {
          setText(e.target.value)
          setSaved(false)
        }}
        className="min-h-12 text-sm"
      />
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" size="sm" variant="outline" disabled={busy || text === plugin.description} aria-busy={busy}>
          {busy && <Spinner />}
          Save description
        </Button>
        <span className="text-[11px] text-muted-foreground">It goes into plugin.json; the next version you publish carries it.</span>
        {saved && (
          <span role="status" className="text-xs text-ok">
            Description saved.
          </span>
        )}
      </div>
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
    </form>
  )
}

function DraftChanges({ diff }: { diff: HostedPluginDraftDiff | null }) {
  if (!diff) return <Skeleton className="h-8 w-full" />
  if (nothingToPublish(diff)) {
    return <p className="text-xs text-muted-foreground">The draft is the same as version {diff.base_version}. Edit it to publish again.</p>
  }
  const rows: { mark: string; tone: string; label: string; paths: string[] }[] = [
    { mark: '+', tone: 'text-ok', label: 'Added', paths: diff.added },
    { mark: '~', tone: 'text-primary', label: 'Changed', paths: diff.changed },
    { mark: '−', tone: 'text-warn', label: 'Removed', paths: diff.removed },
  ]
  return (
    <div className="text-xs">
      <p className="text-muted-foreground">
        {diff.base_version === null ? 'The first version will hold:' : `Changes since version ${diff.base_version}:`}
      </p>
      <ul aria-label="Draft changes" className="mt-1 space-y-0.5">
        {rows.flatMap((row) =>
          row.paths.map((path) => (
            <li key={`${row.mark}${path}`} className="flex min-w-0 gap-2">
              <span aria-label={row.label} className={cn('sb-num w-3 shrink-0 text-center', row.tone)}>
                {row.mark}
              </span>
              <span className="sb-num break-all">{path}</span>
            </li>
          )),
        )}
        {diff.manifest_changed && (
          <li className="flex gap-2">
            <span aria-label="Changed" className="sb-num w-3 shrink-0 text-center text-primary">
              ~
            </span>
            <span>The description (plugin.json)</span>
          </li>
        )}
      </ul>
    </div>
  )
}

function Versions({ versions }: { versions: HostedPluginVersion[] | null }) {
  return (
    <section aria-label="Versions" className="space-y-2">
      <h6 className="text-sm font-medium">Versions</h6>
      {versions === null ? (
        <Skeleton className="h-8 w-full" />
      ) : versions.length === 0 ? (
        <p className="text-xs text-muted-foreground">No version is published. A version never changes once published.</p>
      ) : (
        <ol className="divide-y rounded-lg border text-xs">
          {versions.map((v) => (
            <li key={v.version} className="flex flex-wrap items-baseline gap-x-4 gap-y-0.5 px-3 py-1.5">
              <span className="font-medium">Version {v.version}</span>
              <time dateTime={v.published_at} className="text-muted-foreground">
                {new Date(v.published_at).toLocaleString()}
              </time>
              <span className="text-muted-foreground">by {v.published_by}</span>
              <span className="text-muted-foreground">{Object.keys(v.files).length} files</span>
              <span className="sb-num ml-auto text-muted-foreground" title={v.content_hash}>
                {v.content_hash.replace(/^sha256:/, '').slice(0, 12)}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function OfferPanel({
  plugin,
  pkg,
  onPackage,
}: {
  plugin: HostedPlugin
  pkg: PluginPackage | undefined | null
  onPackage: (pkg: PluginPackage) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<string | null>(null)
  if (pkg === null) {
    return (
      <section aria-label="Assistant" className="space-y-1 rounded-lg border p-3">
        <h6 className="text-sm font-medium">Assistant</h6>
        <p className="text-xs text-muted-foreground">The assistant&rsquo;s packages could not be read, so the offer state is unknown.</p>
      </section>
    )
  }
  const state = offerState(plugin.latest_version, pkg)

  async function offer() {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const next = await aiPlugins.installPackage({ kind: 'hosted', name: plugin.name })
      onPackage(next)
      announcePackagesChanged('hosted')
      const version = next.pending?.version ?? next.version
      setResult(
        next.pending
          ? `Offered version ${version} as a re-pin. Version ${next.version} loads until you approve it.`
          : `Offered version ${version}. It is installed and waits for your approval.`,
      )
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-label="Assistant" className="space-y-2 rounded-lg border p-3">
      <h6 className="text-sm font-medium">Assistant</h6>
      <OfferText state={state} />
      {state.kind === 'not-offered' && (
        <Button type="button" size="sm" onClick={() => void offer()} disabled={busy} aria-busy={busy} {...USER_ONLY}>
          {busy && <Spinner />}
          Offer version {state.version}
        </Button>
      )}
      {result && (
        <p role="status" className="text-xs text-ok">
          {result}
        </p>
      )}
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
      {(state.kind === 'awaiting-approval' || state.kind === 'repin-pending' || state.kind === 'offered' || state.kind === 'name-taken') && (
        <Link to={PACKAGES_HREF} className="inline-block text-xs text-primary hover:underline">
          {state.kind === 'offered' || state.kind === 'name-taken' ? 'Open it in Packages' : 'Review and approve it in Packages'}
        </Link>
      )}
    </section>
  )
}

function OfferText({ state }: { state: OfferState }): ReactNode {
  const line = (text: ReactNode, tone = 'text-muted-foreground') => <p className={cn('text-xs', tone)}>{text}</p>
  switch (state.kind) {
    case 'unpublished':
      return line('Publish a version first. The assistant loads only published versions, after you approve one.')
    case 'not-offered':
      return line(
        state.loaded === null
          ? `Version ${state.version} is not offered. Offer it to install it as a plugin package; it loads nothing until you approve it.`
          : `Version ${state.version} is not offered. The package is pinned to version ${state.loaded}; an offer re-pins it, and version ${state.loaded} loads until you approve the new one.`,
        'text-warn',
      )
    case 'awaiting-approval':
      return line(`Version ${state.version} is installed and waits for your approval.`, 'text-warn')
    case 'repin-pending':
      return line(
        `Version ${state.version} waits for your approval as a re-pin.${state.loaded === null ? '' : ` Version ${state.loaded} loads until then.`}`,
        'text-warn',
      )
    case 'offered':
      return line(
        state.enabled
          ? `The assistant loads version ${state.version}.`
          : `Version ${state.version} is approved. Switch the package on in Packages to load it.`,
      )
    case 'name-taken':
      return line(
        `A plugin package from a ${state.source} source has this name, so this plugin cannot be offered. Remove that package first.`,
        'text-warn',
      )
  }
}

function DeletePlugin({ name, onDeleted }: { name: string; onDeleted: (name: string) => void }) {
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  return (
    <div className="border-t pt-3">
      <Button type="button" size="sm" variant="outline" onClick={() => setConfirm(true)} {...USER_ONLY}>
        Delete plugin
      </Button>
      {error && (
        <p role="alert" className="mt-1 text-xs text-warn">
          {error}
        </p>
      )}
      <AlertDialog open={confirm} onOpenChange={setConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {name}?</AlertDialogTitle>
            <AlertDialogDescription>
              This deletes its draft, every version and its plugin package. The assistant and the plugin feed stop offering it.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep it</AlertDialogCancel>
            <Button
              type="button"
              variant="destructive"
              disabled={busy}
              aria-busy={busy}
              onClick={async () => {
                setBusy(true)
                setError(null)
                try {
                  await hostedPlugins.remove(name)
                  setConfirm(false)
                  announcePackagesChanged('hosted')
                  onDeleted(name)
                } catch (caught) {
                  setConfirm(false)
                  setError(message(caught))
                } finally {
                  setBusy(false)
                }
              }}
              {...USER_ONLY}
            >
              {busy && <Spinner />}
              Delete plugin
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
