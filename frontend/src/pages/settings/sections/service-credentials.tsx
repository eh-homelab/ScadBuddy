import { ServiceCredentials } from '../../../components/auth/ServiceCredentials'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'service-credentials'
export const area: AreaId = 'system'
export const title = 'Service credentials'
export const order = 17

/** #4975 — the credentials the agent and the workers present to the API, with revoke and rotate. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <ServiceCredentials />
    </Section>
  )
}
