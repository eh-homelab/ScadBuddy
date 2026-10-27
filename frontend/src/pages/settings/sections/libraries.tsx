import { LibraryUpgrade } from '../../../components/settings/LibraryUpgrade'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'libraries'
export const area: AreaId = 'library'
export const title = 'Libraries'
export const order = 40

/** Moves a library to another tag or branch across the models that pin it. Never dirty. */
export function Component() {
  return (
    <Section
      id={id}
      title={title}
      description="Move a library to another tag or branch across the models that pin it. Check each model against the candidate first; each ticked model is then re-pinned on its own, as one revision of that model."
    >
      <LibraryUpgrade />
    </Section>
  )
}
