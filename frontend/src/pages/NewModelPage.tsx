import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api/client'
import { SourceWorkbench } from '../components/SourceWorkbench'
import { modelPath } from '../lib/deeplink'
import { detectLibraries } from '../lib/libraryImports'
import { useAsync } from '../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

/** Paste `.scad` source, name it, save it. The schema is derived exactly as on upload. */
export function NewModelPage() {
  const [name, setName] = useState('')
  const [source, setSource] = useState('')
  // #169 — the suggestions are on unless unticked, so only the unticked are kept.
  const [declined, setDeclined] = useState<ReadonlySet<string>>(new Set())
  // No catalogue, no suggestions: the model can still be saved and pinned later.
  const { data: catalogue } = useAsync(() => api.listLibraries(), [])
  // #1291 — the agent's blank starter, with its customizer annotations. Served by
  // the agent service, so with no agent there is no button rather than an error.
  const { data: blank } = useAsync(() => api.getBlankTemplate(), [])
  const detected = useMemo(
    () =>
      detectLibraries(
        source,
        (catalogue ?? []).map((entry) => entry.name),
      ),
    [source, catalogue],
  )

  function toggle(library: string, on: boolean) {
    setDeclined((current) => {
      const next = new Set(current)
      if (on) next.delete(library)
      else next.add(library)
      return next
    })
  }

  async function save(force: boolean) {
    const model = await api.createModelFromSource({
      name: name.trim(),
      source,
      description: '',
      force,
      libraries: detected.filter((library) => !declined.has(library)),
    })
    return modelPath(model.slug)
  }

  return (
    <SourceWorkbench
      breadcrumb={
        <>
          <Link to="/" className="shrink-0 text-xs text-muted-foreground hover:text-foreground">
            Models
          </Link>
          <span className="text-muted-foreground">/</span>
          <h1 className="truncate text-sm font-medium">New model</h1>
        </>
      }
      fields={
        <>
          <div className="flex flex-wrap items-center gap-2 border-t px-3 py-2">
            <Label htmlFor="model-name" className="font-normal text-muted-foreground">
              Name
            </Label>
            <Input
              id="model-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Name Keychain"
              className="h-7 w-64"
            />
            <span className="text-xs text-muted-foreground">
              The URL slug is derived from it, as a filename would be.
            </span>
            {/* Only into an empty editor: it would replace whatever was pasted. */}
            {blank && source === '' && (
              <Button size="sm" variant="outline" className="ml-auto" onClick={() => setSource(blank.source)}>
                Start from blank template
              </Button>
            )}
          </div>
          {detected.length > 0 && (
            <fieldset
              className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t px-3 py-2"
              data-testid="detected-libraries"
            >
              <legend className="float-left mr-2 text-sm text-muted-foreground">Libraries</legend>
              {detected.map((library) => {
                const ref = catalogue?.find((entry) => entry.name === library)?.ref
                return (
                  <Label key={library} className="cursor-pointer font-normal">
                    <Checkbox
                      checked={!declined.has(library)}
                      onCheckedChange={(checked) => toggle(library, checked === true)}
                    />
                    {library}
                    {ref && <span className="font-mono text-xs text-muted-foreground">{ref}</span>}
                  </Label>
                )
              })}
              <span className="text-xs text-muted-foreground">
                Named by the source's use/include lines; pinned to the model when it is saved.
              </span>
            </fieldset>
          )}
        </>
      }
      // Fixed while the name is still being typed: the URI is the editor model's
      // identity, and rebuilding the model on every keystroke would drop undo history.
      // The model is disposed when the page unmounts, so coming back starts blank.
      uri="file:///models/new/model.scad"
      source={source}
      onSourceChange={setSource}
      saveLabel="Save and customize"
      canSave={name.trim().length > 0}
      dirty={source.trim() !== '' || name.trim() !== ''}
      onSave={save}
    />
  )
}
