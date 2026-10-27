import { AiAuditSection } from '../../../components/assistant/AiAuditSection'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'audit-log'
export const area: AreaId = 'audits'
export const title = 'Audit log'
export const order = 10
export const hideWhenEmpty = true

/** Saves on its own (#258); the card shows itself only when the assistant is available. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <AiAuditSection />
    </Section>
  )
}
