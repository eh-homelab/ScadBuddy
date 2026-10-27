import { Link } from 'react-router'
import { useAiAvailability } from '../../../agent/chat/availability'
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
  const ai = useAiAvailability()
  return (
    <Section id={id} title={title}>
      {/* Spec §3.2: AI activity here, background jobs on /activity. Shown with the card, so an
          unavailable assistant still leaves the section empty (hideWhenEmpty). */}
      {ai.available && (
        <p className="text-sm text-muted-foreground">
          Background jobs are on the{' '}
          <Link to="/activity" className="underline decoration-border underline-offset-2 hover:text-primary">
            Activity page
          </Link>
          .
        </p>
      )}
      <AiAuditSection />
    </Section>
  )
}
