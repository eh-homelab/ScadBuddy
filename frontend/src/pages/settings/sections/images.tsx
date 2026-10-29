import { ImageSettingsSetting } from '../../../components/ImageSettingsSetting'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'images'
export const area: AreaId = 'assistant'
export const title = 'Images'
export const order = 60
export const hideWhenEmpty = true

/** Saves on its own; hidden without the agent's database. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <ImageSettingsSetting />
    </Section>
  )
}
