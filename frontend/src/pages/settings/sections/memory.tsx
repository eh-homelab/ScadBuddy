import { useAiAvailability } from '../../../agent/chat/availability'
import { MemorySettingsSetting } from '../../../components/MemorySettingsSetting'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'memory'
export const area: AreaId = 'assistant'
export const title = 'Memory'
export const order = 100
export const hideWhenEmpty = true

/** Saves on its own, read at each turn; shown only where the assistant is available. */
export function Component() {
  const ai = useAiAvailability()
  return <Section id={id} title={title}>{ai.available && <MemorySettingsSetting />}</Section>
}
