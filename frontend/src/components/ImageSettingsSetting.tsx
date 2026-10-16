import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { LabeledInput, SaveFooter, SettingBody, SettingCard } from './settings/form'

/**
 * The long edge, in pixels, the assistant panel scales an attached image down to before
 * sending it, stored by the agent service (`/api/v1/ai/settings/images`). The bounds come
 * with the value: 200 px (smaller images read poorly) to 2576 px (Claude 4.7 and later
 * scale anything larger down). A change applies to images attached from then on.
 *
 * Hidden when the agent service or its database is not there, like the chat limits.
 * Save is user-only: a larger edge spends more image tokens per message.
 */
export function ImageSettingsSetting() {
  const settings = useAsync(() => api.getImageSettings(), [])
  const [edge, setEdge] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (settings.error || !settings.data) return null
  const current = settings.data
  const edgeText = edge ?? String(current.long_edge)
  const changed = Number(edgeText) !== current.long_edge

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      settings.setData(await api.putImageSettings(Number(edgeText)))
      setEdge(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the image setting')
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingCard title="Assistant images">
      <form className="contents" onSubmit={(event) => void save(event)}>
        <SettingBody className="space-y-3">
          <LabeledInput
            label="Image long edge (px)"
            type="number"
            inputMode="numeric"
            min={current.min}
            max={current.max}
            step={1}
            required
            value={edgeText}
            onChange={(event) => setEdge(event.target.value)}
            className="w-36"
            aria-describedby="image-settings-help"
          />
          <p id="image-settings-help" className="text-xs text-muted-foreground">
            Images you attach in the assistant are scaled down to this many pixels on their longer side before they
            are sent. 1568 is the default: models before Claude 4.7 scale anything larger down to it anyway. Claude 4.7
            and later read up to {current.max}, at up to about three times the image tokens. Above 2000, a chat that
            has sent more than 20 images may be refused by the API. Applies to images attached from now on.
          </p>
        </SettingBody>
        <SaveFooter
          saving={saving}
          disabled={!changed}
          saved={saved && !changed ? 'Saved. Images attached from now on use it.' : null}
          error={error}
        />
      </form>
    </SettingCard>
  )
}
