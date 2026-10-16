import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { LabeledInput, SaveFooter, SettingBody, SettingCard } from './settings/form'

const MiB = 1024 * 1024

/** Bytes as whole MiB for the form, rounding a value set some other way. */
const toMiB = (bytes: number) => String(Math.round(bytes / MiB))

/**
 * #2329 — the caps on the assistant's workspace (`/api/v1/ai/settings/workspace`): the
 * largest file, and the most one session's workspace holds. Shown and saved in MiB; the
 * agent stores bytes. A change applies to the next write; files already there stay.
 *
 * Hidden when the agent service or its database is not there, like the image setting.
 */
export function WorkspaceSettingsSetting() {
  const settings = useAsync(() => api.getWorkspaceSettings(), [])
  const [file, setFile] = useState<string | null>(null)
  const [total, setTotal] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (settings.error || !settings.data) return null
  const current = settings.data
  const fileText = file ?? toMiB(current.file_max_bytes)
  const totalText = total ?? toMiB(current.workspace_max_bytes)
  const changed =
    Number(fileText) * MiB !== current.file_max_bytes || Number(totalText) * MiB !== current.workspace_max_bytes

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      settings.setData(
        await api.putWorkspaceSettings({
          file_max_bytes: Number(fileText) * MiB,
          workspace_max_bytes: Number(totalText) * MiB,
        }),
      )
      setFile(null)
      setTotal(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the workspace limits')
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingCard title="Assistant workspace">
      <form className="contents" onSubmit={(event) => void save(event)}>
        <SettingBody className="space-y-3">
          <div className="flex flex-wrap gap-4">
            <LabeledInput
              label="Largest file (MiB)"
              type="number"
              inputMode="numeric"
              min={current.min / MiB}
              max={current.file_max / MiB}
              step={1}
              required
              value={fileText}
              onChange={(event) => setFile(event.target.value)}
              className="w-36"
              aria-describedby="workspace-settings-help"
            />
            <LabeledInput
              label="Per chat, in all (MiB)"
              type="number"
              inputMode="numeric"
              min={current.min / MiB}
              max={current.workspace_max / MiB}
              step={1}
              required
              value={totalText}
              onChange={(event) => setTotal(event.target.value)}
              className="w-36"
              aria-describedby="workspace-settings-help"
            />
          </div>
          <p id="workspace-settings-help" className="text-xs text-muted-foreground">
            The files the assistant keeps for a chat (its downloads, its own files and what you upload under Files) are
            stored in the AI database. 25 MiB a file and 200 MiB a chat are the defaults; a file may be up to{' '}
            {current.file_max / MiB} MiB and a chat&apos;s files up to {current.workspace_max / MiB} MiB. Applies to files
            written from now on.
          </p>
        </SettingBody>
        <SaveFooter saving={saving} disabled={!changed} saved={saved && !changed ? 'Saved.' : null} error={error} />
      </form>
    </SettingCard>
  )
}
