import { SessionLimitsSetting } from '../../../components/SessionLimitsSetting'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'session-limits'
export const area: AreaId = 'assistant'
export const title = 'Session limits'
export const order = 30
export const hideWhenEmpty = true

/** Saves on its own; hidden without the agent's database. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <SessionLimitsSetting />
    </Section>
  )
}
