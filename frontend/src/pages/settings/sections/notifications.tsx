import { useAiAvailability } from '../../../agent/chat/availability'
import { NotifyChannelsSetting } from '../../../components/NotifyChannelsSetting'
import { PushServicesSetting } from '../../../components/PushServicesSetting'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'notifications'
export const area: AreaId = 'assistant'
export const title = 'Notifications'
export const order = 110
export const hideWhenEmpty = true

/** #815 — where the assistant's notifications may go; shown only where the assistant is available. */
export function Component() {
  const ai = useAiAvailability()
  return (
    <Section id={id} title={title}>
      {ai.available && <NotifyChannelsSetting />}
      {ai.available && <PushServicesSetting />}
    </Section>
  )
}
