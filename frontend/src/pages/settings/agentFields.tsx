import { createContext, useContext, useLayoutEffect, useState, type ReactNode, type RefObject } from 'react'
import { useLocation, useNavigate } from 'react-router'
import type { SETTINGS_FIELDS } from '../../agent/catalog'
import { committed, touchAfterRender } from '../../agent/highlight'
import { AgentToolError } from '../../agent/types'
import { useAgentHandlers, useLatest } from '../../agent/useAgentHandlers'
import { ApiError } from '../../api/client'
import type { ConnectionTest } from '../../api/types'
import { useSettingsCache } from './cache'
import { useReveal } from './collapseState'
import { useAnyDirty } from './dirty'
import { resolveLocation, type SettingsSection } from './registry'
import { baseline } from './values'

export type AgentFormField = (typeof SETTINGS_FIELDS)[number]

/** The section that owns each field the agent's settings tools reach, and its input's id. */
export const AGENT_FIELD_HOME: Record<AgentFormField, { section: string; domId: string }> = {
  bambuddy_url: { section: 'connection', domId: 'bambuddy-url' },
  public_url: { section: 'connection', domId: 'public-url' },
  library_folder_id: { section: 'projects', domId: 'library-folder' },
  printer_id: { section: 'printing', domId: 'printer' },
  default_plate: { section: 'preview', domId: 'default-plate' },
  display_unit: { section: 'preview', domId: 'display-unit' },
}

const FIELDS = Object.keys(AGENT_FIELD_HOME) as AgentFormField[]

/** How long set_field waits for the router to open another area (a field edit waits 2 s). */
const AREA_MOVE_TIMEOUT_MS = 10_000

/** How a section lets the agent read and set one of its fields. */
export type AgentFieldBinding = {
  value: () => string
  set: (next: string) => void
  /** The values `set_field` accepts; any value without it. */
  allowed?: () => readonly string[]
  /** The choices `get_form` reports. */
  listed?: () => unknown
}

/** The Connection section's test and its state, for `test_connection` and `get_form`. */
export type ConnectionAgent = {
  testStored: () => Promise<ConnectionTest>
  lastTest: () => ConnectionTest | null
  error: () => string | null
  apiKeyTyped: () => boolean
}

/** What the mounted sections lend the agent: the latest binding of each field, and the test. */
class Bindings {
  readonly fields = new Map<AgentFormField, RefObject<AgentFieldBinding>>()
  private lent: RefObject<ConnectionAgent> | null = null

  connection(): ConnectionAgent | undefined {
    return this.lent?.current
  }

  lend(agent: RefObject<ConnectionAgent>): () => void {
    this.lent = agent
    return () => {
      if (this.lent === agent) this.lent = null
    }
  }
}

const AgentFieldsContext = createContext<Bindings | null>(null)

function useBindings(): Bindings {
  const bindings = useContext(AgentFieldsContext)
  if (!bindings) throw new Error('Settings agent fields outside SettingsAgentProvider')
  return bindings
}

/** Lends `field` to the agent's settings tools while the calling section is mounted. */
export function useSettingsAgentField(field: AgentFormField, binding: AgentFieldBinding): void {
  const { fields } = useBindings()
  const latest = useLatest(binding)
  useLayoutEffect(() => {
    fields.set(field, latest)
    return () => {
      if (fields.get(field) === latest) fields.delete(field)
    }
  }, [fields, field, latest])
}

/** Lends the connection test to `test_connection` while the calling section is mounted. */
export function useSettingsConnectionAgent(agent: ConnectionAgent): void {
  const bindings = useBindings()
  const latest = useLatest(agent)
  useLayoutEffect(() => bindings.lend(latest), [bindings, latest])
}

/**
 * #2171 — the settings form's browser tools (#254) over the Settings areas: each field is
 * read and set through the section that owns it, in whichever area that section lives.
 * They change the form, never what is stored: Save stays the user's (a settings write is
 * outward, AI design spec §8.1), and the API key is neither readable nor settable here.
 */
export function SettingsAgentProvider({ sections, children }: { sections: readonly SettingsSection[]; children: ReactNode }) {
  const [bindings] = useState(() => new Bindings())
  const { settings } = useSettingsCache()
  const anyDirty = useAnyDirty()
  const reveal = useReveal()
  const navigate = useNavigate()
  const location = useLatest(useLocation())

  /** Moves to the area of section `id` when it is not the current one, and waits for the move. */
  async function goTo(id: string) {
    const area = sections.find((section) => section.id === id)?.area
    const current = () => resolveLocation(location.current.pathname, location.current.hash, sections).area
    if (!area || current() === area) return
    navigate(`/settings/${area}`)
    // The router commits the move later; the answer should see the page as the user does.
    // Moving re-renders every area, which a loaded machine takes longer than a field edit.
    await committed(() => current() === area, `the ${area} settings to open`, AREA_MOVE_TIMEOUT_MS)
  }

  function formValues(): Record<AgentFormField, string> {
    return Object.fromEntries(
      FIELDS.map((field) => [field, bindings.fields.get(field)?.current.value() ?? (settings ? baseline(settings, field) : '')]),
    ) as Record<AgentFormField, string>
  }

  function refuseUnlisted(field: AgentFormField, next: string, binding: AgentFieldBinding) {
    const allowed = binding.allowed?.()
    if (allowed && !allowed.includes(next)) {
      throw new AgentToolError(
        'invalid_args',
        `"${next}" is not a choice for ${field}: ${allowed.map((choice) => JSON.stringify(choice)).join(', ')}.`,
      )
    }
  }

  const listed = (field: AgentFormField) => bindings.fields.get(field)?.current.listed?.()

  useAgentHandlers(
    'settings',
    {
      get_form: () => {
        const connection = bindings.connection()
        const test = connection?.lastTest() ?? null
        return {
          values: formValues(),
          unsaved: anyDirty(),
          has_api_key: settings?.has_api_key ?? false,
          api_key_typed: connection?.apiKeyTyped() ?? false,
          choices: {
            library_folder_id: listed('library_folder_id'),
            printer_id: listed('printer_id'),
            default_plate: listed('default_plate'),
            display_unit: listed('display_unit'),
          },
          last_test: test ? { ok: test.ok, detail: test.detail } : null,
          error: connection?.error() ?? null,
        }
      },
      set_field: async ({ field, value: next }) => {
        const home = AGENT_FIELD_HOME[field]
        const binding = bindings.fields.get(field)
        if (!binding) throw new AgentToolError('unavailable', `No settings section here holds ${field}.`)
        refuseUnlisted(field, next, binding.current)
        // Every area is mounted, so the binding is live in a hidden one: the edit is made
        // first, and a slow move to its area never loses it.
        binding.current.set(next)
        // A folded section's field is hidden: unfold it first, so the edit is seen (#3414).
        reveal(home.domId)
        try {
          await goTo(home.section)
        } catch (cause) {
          // The edit stands although its area did not open: say so, or the agent retries blindly.
          if (cause instanceof AgentToolError) {
            throw new AgentToolError(
              cause.code,
              `${cause.message} The edit to ${field} was made in its section's draft, unsaved; do not set it again.`,
            )
          }
          throw cause
        } finally {
          // Once its area shows (or the move gave up), so it can be scrolled into view.
          touchAfterRender(() => document.getElementById(home.domId))
        }
        await committed(() => binding.current.value() === next)
        return { field, value: next, saved: false, note: 'The user saves each section with its own Save button.' }
      },
      test_connection: async () => {
        if (anyDirty()) {
          throw new AgentToolError(
            'refused',
            'The form has unsaved changes, and the test saves the form first. Ask the user to save or test it.',
          )
        }
        const connection = bindings.connection()
        if (!connection) throw new AgentToolError('unavailable', 'No settings section here tests the connection.')
        // With nothing unsaved the stored settings are the form, so this is Test
        // connection minus its save — the one part that would be a settings write.
        touchAfterRender(() => document.querySelector('[data-testid="test-connection"]'))
        try {
          const result = await connection.testStored()
          return { ok: result.ok, detail: result.detail }
        } catch (cause) {
          throw new AgentToolError('failed', cause instanceof ApiError ? cause.detail : 'The connection test could not run.')
        }
      },
    },
    () => ({ values: formValues(), unsaved: anyDirty(), has_api_key: settings?.has_api_key ?? false }),
  )

  return <AgentFieldsContext.Provider value={bindings}>{children}</AgentFieldsContext.Provider>
}
