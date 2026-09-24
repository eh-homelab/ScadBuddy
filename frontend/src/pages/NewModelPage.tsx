import { useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { api } from '../api/client'
import { SourceWorkbench } from '../components/SourceWorkbench'

/** Paste `.scad` source, name it, save it. The schema is derived exactly as on upload. */
export function NewModelPage() {
  const [name, setName] = useState('')
  const [source, setSource] = useState('')
  const navigate = useNavigate()

  async function save(force: boolean) {
    const model = await api.createModelFromSource({ name: name.trim(), source, force })
    await navigate(`/m/${model.slug}`)
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
      }
      // Fixed while the name is still being typed: the URI is the editor model's
      // identity, and rebuilding the model on every keystroke would drop undo history.
      // The model is disposed when the page unmounts, so coming back starts blank.
      uri="file:///models/new/model.scad"
      source={source}
      onSourceChange={setSource}
      saveLabel="Save and customize"
      canSave={name.trim().length > 0}
      onSave={save}
    />
  )
}
