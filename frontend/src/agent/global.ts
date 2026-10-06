import { useContext, useEffect, useRef } from 'react'
import { createPath, UNSAFE_NavigationContext, useLocation, useNavigate } from 'react-router'
import {
  activeDialog,
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
 * Where the router's history is now: it moves the moment a navigation is asked for,
 * before React renders it. The browser and memory histories both expose it; `fallback`
 * for a navigator that does not.
 */
function historyRoute(navigator: object, fallback: string): string {
  const { location } = navigator as { location?: { pathname: string; search: string } }
  return location ? createPath({ pathname: location.pathname, search: location.search }) : fallback
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
  const { navigator } = useContext(UNSAFE_NavigationContext)
  /**
   * The route once this commit's effects have run (#761). Effects run children first,
   * so by the time this one runs, a page that rewrites its own URL in an effect (the
   * catalogue's `view=cards`) has already asked the router to: the history then says
   * where the page is going, and the two differ until that commit lands too.
   */
  const settled = useRef(route)

  useEffect(() => {
    settled.current = route
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
      // Moved, and settled: the page has run its effects on the route and asked for no
      // other (a page that rewrites its URL takes a second commit; answering between
      // the two gave the route before the rewrite).
      const landed = await waitFor(
        () => {
          const now = settled.current
          return now !== from && now === historyRoute(navigator, now) ? now : undefined
        },
        { timeout: 1000, what: 'the route to change' },
      ).catch(() => current.current)
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
      const before = activeDialog()
      setControlValue(element, target)
      const filled = { filled: nameOf(element), value: element.value }
      // A control that asks first (a preset pick over edits, #359) keeps its old value and
      // opens a dialog. Say so, or the agent sees only a value that did not change. Only a
      // dialog this fill opened counts: one already open holds the field itself.
      const dialog = element.value === target ? null : activeDialog()
      if (!dialog || dialog === before) return filled
      return {
        ...filled,
        confirm:
          `"${nameOf(dialog)}" opened instead: the value takes only once it is answered. Take a ` +
          'snapshot to read it. Confirming may discard what is on screen; if the user made those ' +
          'changes, ask them before you confirm.',
      }
    },
  })
}
