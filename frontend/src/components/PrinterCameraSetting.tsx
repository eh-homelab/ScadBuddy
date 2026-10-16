import { useState } from 'react'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { SettingBody, SettingCard, SwitchField } from './settings/form'

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
    <SettingCard title="Printer camera">
      <SettingBody className="space-y-1.5">
        <SwitchField
          label={<>Let the assistant see printers&rsquo; cameras</>}
          checked={enabled}
          disabled={saving}
          busy={saving}
          onCheckedChange={(next) => void toggle(next)}
          describedBy="printer-camera-help"
        />
        <p id="printer-camera-help" className="text-xs text-muted-foreground">
          A camera frame shows whatever is in view of the printer, not only the print. Applies to the
          assistant and to MCP clients; while off, they are told the camera is turned off here.
        </p>
        {error && (
          <p role="alert" className="text-xs text-warn">
            {error}
          </p>
        )}
      </SettingBody>
    </SettingCard>
  )
}
