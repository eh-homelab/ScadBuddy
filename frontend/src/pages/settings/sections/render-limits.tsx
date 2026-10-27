import { RenderLimitsSetting } from '../../../components/RenderLimitsSetting'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'render-limits'
export const area: AreaId = 'assistant'
export const title = 'Render limits'
export const order = 50
export const hideWhenEmpty = true

/** Saves on its own; hidden without the agent's database. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <RenderLimitsSetting />
    </Section>
  )
}
