import { setWebMcpEnabled, useWebMcpEnabled } from '../../../agent/webmcpPreference'
import { BrowserOriginsSetting } from '../../../components/BrowserOriginsSetting'
import { HttpRequestSetting } from '../../../components/HttpRequestSetting'
import { PrinterCameraSetting } from '../../../components/PrinterCameraSetting'
import { SwitchField } from '../../../components/settings/form'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'builtin'
export const area: AreaId = 'tools'
export const title = 'Built-in'
export const order = 10

/** The tools ScadBuddy gives an agent itself; each applies at once. */
export function Component() {
  const webMcp = useWebMcpEnabled()
  return (
    <Section id={id} title={title}>
      {/* Per browser and applied at once. Only the user may flip it: an agent must
          not grant itself access (#254). */}
      <div className="space-y-1.5">
        <SwitchField
          id="webmcp-enabled"
          label={<>Let this browser&rsquo;s built-in agent use ScadBuddy tools (WebMCP)</>}
          checked={webMcp}
          onCheckedChange={setWebMcpEnabled}
          describedBy="webmcp-help"
        />
        <p id="webmcp-help" className="text-xs text-muted-foreground">
          Off by default, as AI design spec §8.5 has an outside agent pair before it drives a tab. Even
          when on, the print and send tool can only open the dialog; you confirm it.
        </p>
      </div>
      <HttpRequestSetting />
      {/* #982 — where the assistant's headless browser may go; hidden without the agent's database. */}
      <BrowserOriginsSetting />
      {/* #1911 — the camera tool's switch; hidden without the agent's database, like the switch above. */}
      <PrinterCameraSetting />
    </Section>
  )
}
