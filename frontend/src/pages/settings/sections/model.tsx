import { ModelSetting } from '../../../components/ModelSetting'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'model'
export const area: AreaId = 'assistant'
export const title = 'Model'
export const order = 20
export const hideWhenEmpty = true

/** Saves on its own; hidden without the agent's database. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <ModelSetting />
    </Section>
  )
}
