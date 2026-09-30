import {
  activeDialog,
  FILLABLE_ROLES,
  interactiveElements,
  isDisabled,
  isUserOnly,
  isVisible,
  nameOf,
  roleOf,
} from './dom'

/** One interactive element, as a screen reader would announce it. */
export interface ElementSummary {
  role: string
  name: string
  value?: string
  checked?: boolean
  selected?: boolean
  disabled?: boolean
  invalid?: boolean
  /** A confirmation only the user can press (send, print, delete, save settings). */
  userOnly?: boolean
}

export interface FieldValue {
  label: string
  role: string
  value: string | boolean
  invalid?: boolean
  error?: string
  /** A select's options, label and value, capped at `MAX_OPTIONS`. */
  options?: { label: string; value: string }[]
  optionsTruncated?: boolean
}

export interface Snapshot {
  route: string
  title: string
  /** Open dialogs, by accessible name; the last is the one on top. */
  dialogs: string[]
  /** Every labelled form control in view (inside the top dialog when one is open). */
  fields: FieldValue[]
  /** Visible `role="alert"` text and the messages of fields marked invalid. */
  errors: string[]
  /** Live `role="status"` text: "Saved …", "Parses cleanly", a test result. */
  status: string[]
  /** What each mounted page reports about itself (the customizer's values, render…). */
  page: Record<string, unknown>
  /** The tools a caller can use right now. */
  tools: string[]
  /** The interactive elements, in document order, capped at `MAX_ELEMENTS`. */
  elements: ElementSummary[]
  truncated: boolean
}

export const MAX_ELEMENTS = 150
export const MAX_OPTIONS = 50
const MAX_TEXT = 300

function clip(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > MAX_TEXT ? `${flat.slice(0, MAX_TEXT)}…` : flat
}

function valueOf(element: Element): string | boolean | undefined {
  // What a user-only control holds (the pairing code, PairingPrompt.tsx) is never
  // read back either: the agent may not fill it, and reading it would hand one agent
  // the code the user is typing to pair another (#746).
  if (isUserOnly(element) && !(element instanceof HTMLInputElement && (element.type === 'checkbox' || element.type === 'radio'))) {
    const typed = element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement
    if (typed) return element.value ? '(typed, hidden)' : ''
  }
  if (element instanceof HTMLInputElement) {
    if (element.type === 'checkbox' || element.type === 'radio') return element.checked
    // A password is never read back, typed or stored.
    if (element.type === 'password') return element.value ? '(typed, hidden)' : ''
    if (element.type === 'file') return element.files?.length ? `${element.files.length} file(s)` : ''
    return element.value
  }
  if (element instanceof HTMLSelectElement) return element.value
  if (element instanceof HTMLTextAreaElement) return element.value
  const ariaValue = element.getAttribute('aria-valuenow') ?? element.getAttribute('aria-valuetext')
  return ariaValue ?? undefined
}

function describe(element: Element): ElementSummary {
  const summary: ElementSummary = { role: roleOf(element), name: clip(nameOf(element)) }
  const value = valueOf(element)
  if (typeof value === 'boolean') summary.checked = value
  else if (value !== undefined && FILLABLE_ROLES.has(summary.role)) summary.value = clip(value)
  const selected = element.getAttribute('aria-selected')
  if (selected !== null) summary.selected = selected === 'true'
  const pressed = element.getAttribute('aria-checked') ?? element.getAttribute('aria-pressed')
  if (pressed !== null) summary.checked = pressed === 'true'
  if (isDisabled(element)) summary.disabled = true
  if (element.getAttribute('aria-invalid') === 'true') summary.invalid = true
  if (isUserOnly(element)) summary.userOnly = true
  return summary
}

function errorFor(element: Element): string | undefined {
  const ids = element.getAttribute('aria-errormessage') ?? element.getAttribute('aria-describedby')
  if (!ids) return undefined
  const text = ids
    .split(/\s+/)
    .map((id) => element.ownerDocument.getElementById(id)?.textContent ?? '')
    .join(' ')
  return clip(text) || undefined
}

function texts(scope: ParentNode, selector: string): string[] {
  return [...scope.querySelectorAll(selector)]
    .filter(isVisible)
    .map((node) => clip(node.textContent ?? ''))
    .filter(Boolean)
}

/**
 * The agent's "screen": the same things a user would glance at. Values come from the
 * DOM, which React has already rendered from state, so what the agent reads is what
 * the user sees.
 */
export function takeSnapshot({
  route,
  page,
  tools,
  root = document,
}: {
  route: string
  page: Record<string, unknown>
  tools: string[]
  root?: Document
}): Snapshot {
  const dialogs = [...root.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open]')]
    .filter(isVisible)
    .map((dialog) => clip(nameOf(dialog)) || '(untitled dialog)')
  const scope: ParentNode = activeDialog(root) ?? root.body

  const controls = interactiveElements(scope)
  const fields: FieldValue[] = []
  const errors = texts(scope, '[role="alert"]')
  for (const element of controls) {
    const role = roleOf(element)
    if (!FILLABLE_ROLES.has(role) && role !== 'checkbox' && role !== 'switch') continue
    // Monaco's hidden input is not a field; the source tools read the editor.
    if (element.closest('.monaco-editor')) continue
    const label = clip(nameOf(element))
    if (!label) continue
    const value = valueOf(element)
    const field: FieldValue = {
      label,
      role,
      value: typeof value === 'string' ? clip(value) : (value ?? ''),
    }
    if (element instanceof HTMLSelectElement && !isUserOnly(element)) {
      const options = [...element.options].map((option) => ({ label: clip(option.text), value: clip(option.value) }))
      field.options = options.slice(0, MAX_OPTIONS)
      if (options.length > MAX_OPTIONS) field.optionsTruncated = true
    }
    if (element.getAttribute('aria-invalid') === 'true' || (element as HTMLInputElement).validity?.valid === false) {
      field.invalid = true
      const message = errorFor(element) ?? ((element as HTMLInputElement).validationMessage || undefined)
      if (message) {
        field.error = message
        errors.push(`${label}: ${message}`)
      }
    }
    fields.push(field)
  }

  const elements = controls.map(describe).filter((entry) => entry.name || entry.role !== 'generic')
  return {
    route,
    title: root.title,
    dialogs,
    fields,
    errors,
    status: texts(scope, '[role="status"]'),
    page,
    tools,
    elements: elements.slice(0, MAX_ELEMENTS),
    truncated: elements.length > MAX_ELEMENTS,
  }
}
