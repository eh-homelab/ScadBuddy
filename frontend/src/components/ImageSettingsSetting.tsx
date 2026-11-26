import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { LabeledInput, SaveFooter, SettingBody, SettingCard } from './settings/form'

/**
 * The assistant's image settings, stored by the agent service (`/api/v1/ai/settings/images`).
 * The long edge, in pixels, the panel scales an attached image down to before sending it:
 * 200 px (smaller images read poorly) to 2576 px (Claude 4.7 and later scale anything larger
 * down); it applies to images attached from then on. And the largest image, in KB, a tool
 * shows the assistant inline (#7745): a larger one is kept in the chat's files, where the
 * assistant opens it only when it needs to. The bounds come with the values.
 *
 * Hidden when the agent service or its database is not there, like the chat limits.
 * Save is user-only: a larger edge spends more image tokens per message.
 */
export function ImageSettingsSetting() {
  const settings = useAsync(() => api.getImageSettings(), [])
  const [edge, setEdge] = useState<string | null>(null)
  const [inline, setInline] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (settings.error || !settings.data) return null
  const current = settings.data
  const edgeText = edge ?? String(current.long_edge)
  const edgeChanged = Number(edgeText) !== current.long_edge
  const inlineKb = Math.round(current.tool_inline_max_bytes / 1024)
  const inlineText = inline ?? String(inlineKb)
  const inlineChanged = Number(inlineText) !== inlineKb
  const changed = edgeChanged || inlineChanged

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      settings.setData(
        await api.putImageSettings({
          ...(edgeChanged ? { long_edge: Number(edgeText) } : {}),
          ...(inlineChanged ? { tool_inline_max_bytes: Number(inlineText) * 1024 } : {}),
        }),
      )
      setEdge(null)
      setInline(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the image settings')
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
          <LabeledInput
            label="Largest image the assistant sees at once (KB)"
            type="number"
            inputMode="numeric"
            min={Math.ceil(current.tool_inline_min / 1024)}
            max={Math.floor(current.tool_inline_max / 1024)}
            step={1}
            required
            value={inlineText}
            onChange={(event) => setInline(event.target.value)}
            className="w-36"
            aria-describedby="image-inline-help"
          />
          <p id="image-inline-help" className="text-xs text-muted-foreground">
            When the assistant opens a photo or picture, such as a print photo or a spool label, one this size or
            smaller goes straight into the chat. A larger one is kept in the chat's files, and the assistant opens it
            from there when it needs it, so large photos do not fill the chat. 512 is the default. Applies to the next
            picture the assistant opens.
          </p>
        </SettingBody>
        <SaveFooter
          saving={saving}
          disabled={!changed}
          saved={saved && !changed ? 'Saved.' : null}
          error={error}
        />
      </form>
    </SettingCard>
  )
}
