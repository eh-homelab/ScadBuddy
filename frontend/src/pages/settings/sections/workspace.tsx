import { WorkspaceSettingsSetting } from '../../../components/WorkspaceSettingsSetting'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'workspace'
export const area: AreaId = 'assistant'
export const title = 'Workspace'
export const order = 70
export const hideWhenEmpty = true

/** Saves on its own; hidden without the agent's database. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <WorkspaceSettingsSetting />
    </Section>
  )
}
