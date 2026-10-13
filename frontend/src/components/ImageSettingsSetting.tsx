import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

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
    <section className="mt-4 rounded-[6px] border border-line bg-surface">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Assistant images</h2>
      <form className="flex flex-col gap-3 p-4" onSubmit={(event) => void save(event)}>
        <label className="flex flex-col gap-1 text-[13px]">
          Image long edge (px)
          <input
            type="number"
            inputMode="numeric"
            min={current.min}
            max={current.max}
            step={1}
            required
            value={edgeText}
            onChange={(event) => setEdge(event.target.value)}
            className="sb-field sb-num w-32"
            aria-describedby="image-settings-help"
          />
        </label>
        <p id="image-settings-help" className="text-[12px] text-muted">
          Images you attach in the assistant are scaled down to this many pixels on their longer side before they
          are sent. 1568 is the default: models before Claude 4.7 scale anything larger down to it anyway. Claude 4.7
          and later read up to {current.max}, at up to about three times the image tokens. Above 2000, a chat that
          has sent more than 20 images may be refused by the API. Applies to images attached from now on.
        </p>
        <div className="flex items-center gap-3">
          <Button type="submit" variant="primary" size="sm" disabled={!changed || saving} aria-busy={saving} {...USER_ONLY}>
            {saving && <Spinner />}
            Save
          </Button>
          {saved && !changed && (
            <span role="status" className="text-[12px] text-ok">
              Saved. Images attached from now on use it.
            </span>
          )}
        </div>
        {error && (
          <p role="alert" className="text-[12px] text-warn">
            {error}
          </p>
        )}
      </form>
    </section>
  )
}
