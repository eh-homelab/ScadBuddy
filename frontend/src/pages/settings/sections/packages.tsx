import { useAiAvailability } from '../../../agent/chat/availability'
import { PluginPackagesPanel } from '../../../components/settings/PluginPackages'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'packages'
export const area: AreaId = 'tools'
export const title = 'Packages'
export const order = 20
export const hideWhenEmpty = true

/** Each action is its own request, applied at once; shown only where the assistant is available. */
export function Component() {
  const ai = useAiAvailability()
  return (
    <Section id={id} title={title}>
      {ai.available && (
        <div>
          <p className="text-sm font-medium">Assistant plugins</p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            What the assistant can load besides ScadBuddy&rsquo;s own tools. Each change applies at once.
          </p>
          <PluginPackagesPanel />
        </div>
      )}
    </Section>
  )
}
