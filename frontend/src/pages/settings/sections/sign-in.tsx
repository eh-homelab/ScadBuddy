import { LoginSettings } from '../../../components/auth/LoginSettings'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'sign-in'
export const area: AreaId = 'system'
export const title = 'Sign-in'
export const order = 15

/** #4975 — ScadBuddy's own login; saved on its own, with a discovery check first. */
export function Component() {
  return (
    <Section id={id} title={title}>
      <LoginSettings />
    </Section>
  )
}
