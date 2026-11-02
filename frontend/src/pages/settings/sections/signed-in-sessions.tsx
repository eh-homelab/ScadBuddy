import { SignedInSessions } from '../../../components/auth/SignedInSessions'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'signed-in-sessions'
export const area: AreaId = 'system'
export const title = 'Signed-in sessions'
export const order = 16

/** #4975 — where this person is signed in, with revoke and Sign out everywhere. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <SignedInSessions />
    </Section>
  )
}
