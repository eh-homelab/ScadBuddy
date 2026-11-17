import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { settings as fixture, settingDocs } from '../../mocks/fixtures'
import { RUNTIME_FIELDS, envVar, type SectionId } from './fields'
import { SECTION_REGISTRY } from './registry'
import { renderSettingsSection } from './testing'
import { FETCH_NOWHERE, FOLLOW_NOBODY, HAND_LAID, fieldsOf } from './values'

/** Every section that saves settings through the form (`HAND_LAID`, `RUNTIME_FIELDS`). */
const SECTIONS = [
  ...new Set<SectionId>([...(Object.keys(HAND_LAID) as SectionId[]), ...RUNTIME_FIELDS.map((spec) => spec.section)]),
]

describe('settings field docs (#887)', () => {
  it('has a title and a description from the backend for every field a section saves', () => {
    for (const section of SECTIONS) {
      for (const name of fieldsOf(section, fixture)) {
        const doc = settingDocs[name]
        expect(doc, `${section}: ${name} has no doc in core/setting_docs.py`).toBeDefined()
        expect(doc?.title.trim(), name).toBeTruthy()
        expect(doc?.description.trim(), name).toBeTruthy()
      }
    }
  })

  it('names the env var of each env-seeded field, and only those', () => {
    for (const name of Object.keys(fixture.applies ?? {})) {
      expect(settingDocs[name]?.env_var, name).toBe(envVar(name))
    }
    expect(settingDocs.display_unit?.env_var ?? null).toBeNull()
  })

  it('spells the words the form takes as the help does', () => {
    expect(settingDocs.activity_follow?.description).toContain(`"${FOLLOW_NOBODY}"`)
    expect(settingDocs.asset_fetch_domains?.description).toContain(`"${FETCH_NOWHERE}"`)
  })

  it('shows no field without a label and help, and the env var under each env-backed one', async () => {
    const sections = SECTION_REGISTRY.filter((section) => (SECTIONS as string[]).includes(section.id))
    expect(sections.map((section) => section.id).sort()).toEqual([...SECTIONS].sort())
    renderSettingsSection(
      <>
        {sections.map(({ id, Component }) => (
          <Component key={id} />
        ))}
      </>,
      { sections },
    )
    await screen.findByLabelText('Render timeout')
    await waitFor(() => expect(document.querySelectorAll('[data-field-row]').length).toBeGreaterThan(80))
    const rows = [...document.querySelectorAll<HTMLElement>('[data-field-row]')]
    for (const row of rows) {
      const id = row.dataset.fieldRow!
      const label = row.querySelector(`label[for="${CSS.escape(id)}"]`)
      expect(label?.textContent?.trim(), `${id} has no label`).toBeTruthy()
      expect(label?.textContent, `${id} is labelled by its raw name`).not.toMatch(/^SCADBUDDY_|^[a-z0-9]+(_[a-z0-9]+)+$/)
      expect(document.getElementById(`${id}-help`)?.textContent?.trim(), `${id} has no help`).toBeTruthy()
    }
    // Every runtime row is env-seeded, so each shows its variable.
    for (const spec of RUNTIME_FIELDS) {
      expect(screen.getByTestId(`env-setting-${spec.name}`)).toHaveTextContent(envVar(spec.name))
    }
    expect(screen.getByTestId('env-bambuddy-render-key')).toHaveTextContent('SCADBUDDY_BAMBUDDY_RENDER_API_KEY')
    // A setting only Settings stores has no variable to show.
    expect(screen.queryByTestId('env-display-unit')).not.toBeInTheDocument()
  })
})
