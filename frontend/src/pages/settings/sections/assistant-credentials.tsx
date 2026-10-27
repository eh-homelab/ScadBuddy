import { AiCredentialSection } from '../../../components/assistant/AiCredentialSection'
import { AiStatusSection } from '../../../components/assistant/AiStatusSection'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'assistant-credentials'
export const area: AreaId = 'assistant'
export const title = 'Status & credentials'
export const order = 10

/** Applied at once. Not behind `ai.available`: a missing credential is why the assistant is off (#1000). */
export function Component() {
  return (
    <Section id={id} title={title} description="Applied at once; not part of any saved section.">
      <AiStatusSection />
      <AiCredentialSection />
    </Section>
  )
}
