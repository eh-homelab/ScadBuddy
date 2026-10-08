import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api/client'
import { SourceWorkbench } from '../components/SourceWorkbench'
import { modelPath } from '../lib/deeplink'
import { detectLibraries } from '../lib/libraryImports'
import { useAsync } from '../lib/useAsync'

/** Paste `.scad` source, name it, save it. The schema is derived exactly as on upload. */
export function NewModelPage() {
  const [name, setName] = useState('')
  const [source, setSource] = useState('')
  // #169 — the suggestions are on unless unticked, so only the unticked are kept.
  const [declined, setDeclined] = useState<ReadonlySet<string>>(new Set())
  // No catalogue, no suggestions: the model can still be saved and pinned later.
  const { data: catalogue } = useAsync(() => api.listLibraries(), [])
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
          <Link to="/" className="shrink-0 text-[12px] text-muted hover:text-ink">
            Models
          </Link>
          <span className="text-faint">/</span>
          <h1 className="truncate text-[13px] font-medium">New model</h1>
        </>
      }
      fields={
        <>
          <div className="flex items-center gap-2 border-t border-line px-3 py-2">
            <label htmlFor="model-name" className="text-[13px] text-muted">
              Name
            </label>
            <input
              id="model-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Name Keychain"
              className="h-7 w-64 rounded-[6px] border border-line bg-surface-2 px-2 text-[13px] outline-none focus:border-line-strong"
            />
            <span className="text-[12px] text-faint">
              The URL slug is derived from it, as a filename would be.
            </span>
          </div>
          {detected.length > 0 && (
            <fieldset
              className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line px-3 py-2"
              data-testid="detected-libraries"
            >
              <legend className="float-left mr-2 text-[13px] text-muted">Libraries</legend>
              {detected.map((library) => {
                const ref = catalogue?.find((entry) => entry.name === library)?.ref
                return (
                  <label
                    key={library}
                    className="flex cursor-pointer items-center gap-2 text-[13px]"
                  >
                    <input
                      type="checkbox"
                      checked={!declined.has(library)}
                      onChange={(event) => toggle(library, event.target.checked)}
                      className="accent-[var(--sb-accent)]"
                    />
                    {library}
                    {ref && <span className="font-mono text-[12px] text-faint">{ref}</span>}
                  </label>
                )
              })}
              <span className="text-[12px] text-faint">
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
