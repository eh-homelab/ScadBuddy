import { SessionModeSetting } from '../../../components/SessionModeSetting'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'default-session-mode'
export const area: AreaId = 'assistant'
export const title = 'Session mode'
export const order = 80
export const hideWhenEmpty = true

/** Saves on its own; hidden without the agent's database. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <SessionModeSetting />
    </Section>
  )
}
