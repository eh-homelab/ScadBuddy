import { SessionRuntimeSetting } from '../../../components/SessionRuntimeSetting'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'session-runtime'
export const area: AreaId = 'assistant'
export const title = 'Runtime'
export const order = 40
export const hideWhenEmpty = true

/** Saves on its own; hidden without the agent's database. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <SessionRuntimeSetting />
    </Section>
  )
}
