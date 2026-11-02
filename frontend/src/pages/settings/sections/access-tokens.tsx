import { AccessTokens } from '../../../components/auth/AccessTokens'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'access-tokens'
export const area: AreaId = 'system'
export const title = 'Access tokens'
export const order = 16.5

/** #4975 phase 6 — a signed-in person's personal access tokens, for bambuddy-fs and the Go client. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <AccessTokens />
    </Section>
  )
}
