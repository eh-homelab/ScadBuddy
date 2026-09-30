import { useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router'
import {
  findByRole,
  findField,
  isDisabled,
  isUserOnly,
  nameOf,
  roleOf,
  setControlValue,
} from './dom'
import { touch, waitFor } from './highlight'
import { AgentToolError } from './types'
import { useAgentBridge, useAgentHandlers, useLatest } from './useAgentHandlers'

const USER_ONLY_MESSAGE =
  'Only the user can press this: it confirms an action that leaves ScadBuddy or cannot be ' +
  'undone (send, print, delete, save settings). Ask the user to review it and press it.'

/** `navigate` accepts in-app paths only: no scheme, no protocol-relative `//host`. */
function checkRoute(route: string): string {
  if (!route.startsWith('/') || route.startsWith('//') || route.includes('\\')) {
    throw new AgentToolError('invalid_args', `"${route}" is not an in-app path; it must start with a single "/".`)
  }
  return route
}

/**
 * The option of a select that `value` names: its value, or else the one option whose
 * visible label it is (the snapshot and the screen show labels, and a preset's value
 * can be an opaque id).
 */
function optionValue(select: HTMLSelectElement, label: string, value: string): string {
  const options = [...select.options]
  if (options.some((option) => option.value === value)) return value
  const byLabel = options.filter((option) => option.text.trim() === value.trim())
  if (byLabel.length === 1) return (byLabel[0] as HTMLOptionElement).value
  const listed = options.map((option) => `${JSON.stringify(option.text.trim())} (${JSON.stringify(option.value)})`).join(', ')
  throw new AgentToolError(
    'invalid_args',
    byLabel.length > 1
      ? `${byLabel.length} options of "${label}" are labelled "${value}"; pass one's value: ${listed}.`
      : `"${value}" is not one of the options of "${label}" (label (value)): ${listed}.`,
  )
}

/**
 * The tools every route has — `navigate`, `snapshot` and the fallbacks `click` and
 * `fill` — registered by the app shell, which is mounted under the router for as long as
 * the app is. It also tells the bridge where the router is.
 */
export function useGlobalAgentTools() {
  const bridge = useAgentBridge()
  const navigate = useNavigate()
  const location = useLocation()
  const route = `${location.pathname}${location.search}`
  const current = useLatest(route)

  useEffect(() => {
    bridge.setRoute(route)
  }, [bridge, route])

  useAgentHandlers('app', {
    navigate: async ({ route: target }) => {
      const from = current.current
      await navigate(checkRoute(target))
      if (target === from) return { route: from }
      // Wait for the router to commit, so the next call sees the new page's tools. A
      // redirect (an unknown path lands on "/") still counts as having moved.
      // One that redirects straight back to where it started never changes it, so the
      // wait is short and its end is an answer, not an error.
      const landed = await waitFor(() => (current.current !== from ? current.current : undefined), {
        timeout: 1000,
        what: 'the route to change',
      }).catch(() => current.current)
      return { route: landed }
    },

    snapshot: () => bridge.snapshot(),

    click: ({ role, name, index }) => {
      const matches = findByRole(role, name)
      if (matches.length === 0) {
        throw new AgentToolError('invalid_args', `No visible ${role} named "${name}". Take a snapshot to see what is on screen.`)
      }
      if (matches.length > 1 && index === undefined) {
        throw new AgentToolError(
          'invalid_args',
          `${matches.length} visible ${role}s are named "${name}"; pass index (0–${matches.length - 1}).`,
        )
      }
      const element = matches[index ?? 0]
      if (!element) throw new AgentToolError('invalid_args', `index ${index} is out of range (${matches.length} matches).`)
      if (isUserOnly(element)) throw new AgentToolError('refused', USER_ONLY_MESSAGE)
      if (isDisabled(element)) throw new AgentToolError('invalid_args', `The ${role} "${name}" is disabled.`)
      touch(element)
      ;(element as HTMLElement).click()
      return { clicked: { role: roleOf(element), name: nameOf(element) } }
    },

    fill: ({ label, value }) => {
      const matches = findField(label)
      if (matches.length === 0) {
        throw new AgentToolError('invalid_args', `No visible field labelled "${label}". Take a snapshot to see the fields.`)
      }
      if (matches.length > 1) {
        throw new AgentToolError('invalid_args', `${matches.length} visible fields are labelled "${label}".`)
      }
      const element = matches[0] as Element
      if (isUserOnly(element) || (element instanceof HTMLInputElement && element.type === 'password')) {
        throw new AgentToolError('refused', 'Credentials are for the user to type; an agent never fills them.')
      }
      if (element.closest('.monaco-editor')) {
        throw new AgentToolError('invalid_args', 'Use replace_range to edit the source.')
      }
      if (
        !(element instanceof HTMLInputElement) &&
        !(element instanceof HTMLTextAreaElement) &&
        !(element instanceof HTMLSelectElement)
      ) {
        throw new AgentToolError('invalid_args', `"${label}" is not a field fill can type into.`)
      }
      if (isDisabled(element) || (element as HTMLInputElement).readOnly) {
        throw new AgentToolError('invalid_args', `"${label}" is not editable.`)
      }
      const target = element instanceof HTMLSelectElement ? optionValue(element, label, value) : value
      touch(element)
      setControlValue(element, target)
      return { filled: nameOf(element), value: element.value }
    },
  })
}
