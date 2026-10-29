import { DurableSubagentsSetting } from '../../../components/DurableSubagentsSetting'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'durable-subagents'
export const area: AreaId = 'assistant'
export const title = 'Subagents'
export const order = 90
export const hideWhenEmpty = true

/** Saves on its own; hidden without the agent's database. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <DurableSubagentsSetting />
    </Section>
  )
}
