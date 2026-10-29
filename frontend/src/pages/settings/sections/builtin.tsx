import { useAiAvailability } from '../../../agent/chat/availability'
import { setWebMcpEnabled, useWebMcpEnabled } from '../../../agent/webmcpPreference'
import { ApiError } from '../../../api/client'
import type { SourceView } from '../../../api/capabilities'
import { BrowserOriginsSetting } from '../../../components/BrowserOriginsSetting'
import { SourceCard } from '../../../components/capabilities/SourceCard'
import { useCapabilities } from '../../../components/capabilities/useCapabilities'
import { SwitchField } from '../../../components/settings/form'
import { Section } from '../controls'
import type { AreaId } from '../registry'
import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'

export const id = 'builtin'
export const area: AreaId = 'tools'
export const title = 'Built-in'
export const order = 10

/**
 * The built-in sources this section shows, in order. `builtin:own-plugin`'s switch stays on
 * the Packages section until #4757.
 */
const SHOWN = ['builtin:scadbuddy', 'builtin:browser', 'builtin:http'] as const
const shown = (source: SourceView) => (SHOWN as readonly string[]).includes(source.id)
const rank = (source: SourceView) => (SHOWN as readonly string[]).indexOf(source.id)

/** The tools ScadBuddy gives an agent itself (#2171); each change applies at once. */
export function Component() {
  const ai = useAiAvailability()
  return (
    <Section id={id} title={title}>
      {ai.available && <BuiltInSources />}
      <WebMcpCard />
    </Section>
  )
}

/** Each built-in source's tools; nothing without the agent's database (503). */
function BuiltInSources() {
  const { sources, error, replace } = useCapabilities(shown)
  if (error instanceof ApiError && error.status === 503) return null
  if (error) {
    return (
      <p role="alert" className="text-xs text-warn">
        Could not load the built-in tools: {error.message}
      </p>
    )
  }
  return (
    <div className="flex flex-col gap-2">
      {[...sources]
        .sort((a, b) => rank(a) - rank(b))
        .map((source) => (
          <SourceCard key={source.id} source={source} onChange={replace}>
            {/* #982: where the assistant's headless browser may go. */}
            {source.id === 'builtin:browser' && <BrowserOriginsSetting />}
          </SourceCard>
        ))}
    </div>
  )
}

const WEBMCP_TITLE = 'This browser’s agent (WebMCP)'

/** Per browser (localStorage), so it shows without the agent too. */
function WebMcpCard() {
  const webMcp = useWebMcpEnabled()
  return (
    <Card size="sm" className="mt-2 px-3" role="region" aria-label={WEBMCP_TITLE}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">{WEBMCP_TITLE}</p>
        <Badge variant="outline">This browser only</Badge>
      </div>
      {/* Only the user may flip it: it admits an outside in-browser agent, which pairs
          first (AI spec §8.5), not a tool's access (#254). */}
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
    </Card>
  )
}
