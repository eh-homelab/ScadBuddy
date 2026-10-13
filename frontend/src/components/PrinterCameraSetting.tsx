import { useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { Spinner } from './ui/Spinner'

/**
 * #1911 — lets the AI assistant and MCP clients see a printer's camera (the
 * `get_printer_camera` tool). A camera frame shows whatever is in view, so it is
 * privacy-sensitive (#251). Stored by the agent service
 * (`/api/v1/ai/settings/printer-camera`), on by default; while off, every call to the
 * tool is refused.
 *
 * Hidden when the agent service or its database is not there, like the HTTP request
 * switch. Only the user may flip it: an agent must not grant itself access (AI design
 * spec §8.1).
 */
export function PrinterCameraSetting() {
  const setting = useAsync(() => api.getPrinterCameraSetting(), [])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (setting.error || !setting.data) return null
  const enabled = setting.data.enabled

  async function toggle(next: boolean) {
    setSaving(true)
    setError(null)
    try {
      setting.setData(await api.putPrinterCameraSetting(next))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the setting')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Printer camera</h2>
      <div className="p-4">
        <label className="flex items-start gap-2 text-[13px]" {...USER_ONLY}>
          <input
            type="checkbox"
            checked={enabled}
            disabled={saving}
            aria-busy={saving}
            onChange={(event) => void toggle(event.target.checked)}
            className="mt-0.5"
            aria-describedby="printer-camera-help"
          />
          Let the assistant see printers&rsquo; cameras
          {saving && <Spinner />}
        </label>
        <p id="printer-camera-help" className="mt-1.5 text-[12px] text-muted">
          A camera frame shows whatever is in view of the printer, not only the print. Applies to the
          assistant and to MCP clients; while off, they are told the camera is turned off here.
        </p>
        {error && (
          <p role="alert" className="mt-1.5 text-[12px] text-warn">
            {error}
          </p>
        )}
      </div>
    </section>
  )
}
