import { useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import type { CustomizerSchema, FontFamily, ParamValue } from '../api/types'
import { diffFromDefaults, extrudersIn, extrudersOf, groupsOf, type ParamValues } from '../lib/params'
import { ParamWidget } from './widgets/ParamWidget'
import { Button } from './ui/Button'

const GLOBAL_GROUP = 'Global'

interface Props {
  schema: CustomizerSchema
  /** The model being customized; `file` parameters upload against it (#204). */
  slug: string
  /** The revision being customized, if not the current one (#90). */
  version?: string
  values: ParamValues
  fonts: FontFamily[]
  onChange: (name: string, value: ParamValue) => void
  onReset: () => void
  /** Above the group tabs: the preset picker. */
  toolbar?: ReactNode
  /**
   * #254 — a parameter an agent just changed. A new object switches to its tab, so the
   * change happens where the user can see it.
   */
  reveal?: { name: string }
  /**
   * #938 — the latest finished render: the colours it used, in extruder order, and the
   * values it ran with. With it a colour parameter unchanged since is labelled with the
   * extruder it actually got, or as not in the render; without it, by its place among
   * the colour parameters.
   */
  rendered?: { colors: string[]; params: ParamValues }
  /**
   * #971 — on a short stacked window (and #1741 at a phone's width) the page scrolls, so
   * the list takes its full height rather than scrolling in a box. Off where the panel
   * has a height of its own (the full-screen flyout).
   */
  growsWithPage?: boolean
}

export function ParameterPanel({
  schema,
  slug,
  version,
  values,
  fonts,
  onChange,
  onReset,
  toolbar,
  reveal,
  rendered,
  growsWithPage = false,
}: Props) {
  const groups = useMemo(() => groupsOf(schema), [schema])
  const tabs = useMemo(() => groups.filter((group) => group.name !== GLOBAL_GROUP), [groups])
  const globalGroup = groups.find((group) => group.name === GLOBAL_GROUP)
  const [active, setActive] = useState(() => tabs[0]?.name ?? GLOBAL_GROUP)
  const [revealed, setRevealed] = useState(reveal)
  // Adjusting state when a prop changes, during render rather than in an effect, so the
  // right tab is in the same commit as the change instead of one frame late:
  // https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes
  if (reveal !== revealed) {
    setRevealed(reveal)
    const home = reveal && tabs.find((group) => group.params.some((param) => param.name === reveal.name))
    if (home) setActive(home.name)
  }
  const current = tabs.find((group) => group.name === active) ?? tabs[0]

  // #968 — the WAI-ARIA tabs pattern: the tablist is one Tab stop (the selected tab), and
  // the arrow keys, Home and End move between tabs, selecting each as it is reached.
  const ids = useId()
  const tabId = (index: number) => `${ids}-tab-${index}`
  const panelId = `${ids}-panel`
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])
  const currentIndex = current ? tabs.indexOf(current) : -1
  function onTabKey(event: KeyboardEvent) {
    const last = tabs.length - 1
    const next =
      event.key === 'ArrowRight'
        ? currentIndex === last ? 0 : currentIndex + 1
        : event.key === 'ArrowLeft'
          ? currentIndex <= 0 ? last : currentIndex - 1
          : event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? last
              : null
    const group = next === null ? undefined : tabs[next]
    if (next === null || !group) return
    event.preventDefault()
    setActive(group.name)
    tabRefs.current[next]?.focus()
  }

  const extruders = useMemo<Map<string, number | null | undefined>>(
    () =>
      rendered ? extrudersIn(schema, values, rendered.colors, rendered.params) : extrudersOf(schema, values),
    [schema, values, rendered],
  )
  const extruderOf = (name: string) => extruders.get(name)

  // The font picker previews what will actually be printed, so it needs the model's
  // own text: the first string parameter, which on the keychain is the name.
  const sampleText = useMemo(() => {
    const text = (schema.parameters ?? []).find((param) => param.type === 'string')
    return text ? String(values[text.name] ?? text.initial ?? '') : ''
  }, [schema, values])

  const changed = diffFromDefaults(schema, values).length
  const isDefault = changed === 0

  return (
    <section
      aria-label="Parameters"
      className="flex h-full min-h-0 w-full flex-col border-r border-line bg-surface"
    >
      {toolbar}
      <div
        role="tablist"
        onKeyDown={onTabKey}
        aria-label="Parameter groups"
        // #942 — wrapped onto rows, never scrolled: a template with many groups (Dollhouse
        // Kit has 15) hid most of them past an edge with no scrollbar, fade or arrow.
        className="flex shrink-0 flex-wrap gap-x-0.5 gap-y-1 border-b border-line px-2 pt-2"
      >
        {tabs.map((group, index) => {
          const selected = group.name === current?.name
          return (
            <button
              key={group.name}
              ref={(element) => {
                tabRefs.current[index] = element
              }}
              id={tabId(index)}
              role="tab"
              type="button"
              aria-selected={selected}
              // One panel, showing the selected group: every tab controls it.
              aria-controls={panelId}
              tabIndex={selected ? 0 : -1}
              onClick={() => setActive(group.name)}
              className={`-mb-px shrink-0 border-b-2 px-2.5 pb-2 text-[13px] transition-colors ${
                selected
                  ? 'border-accent text-ink'
                  : 'border-transparent text-muted hover:text-ink'
              }`}
            >
              {group.name}
            </button>
          )
        })}
      </div>

      <div className={`min-h-0 flex-1 overflow-y-auto ${growsWithPage ? 'short:flex-none phone:flex-none' : ''}`}>
        {globalGroup && (
          <div className="border-b border-line bg-surface-2/40">
            <ul className="divide-y divide-line/60">
              {globalGroup.params.map((param) => (
                <li key={param.name} data-param={param.name}>
                  <ParamWidget
                    param={param}
                    value={values[param.name] ?? (param.initial as ParamValue)}
                    slug={slug}
                    version={version}
                    fonts={fonts}
                    sampleText={sampleText}
                    extruder={extruderOf(param.name)}
                    onChange={(next) => onChange(param.name, next)}
                  />
                </li>
              ))}
            </ul>
          </div>
        )}

        {current && (
          <ul id={panelId} role="tabpanel" aria-labelledby={tabId(currentIndex)} className="divide-y divide-line/60">
            {current.params.map((param) => (
              <li key={param.name} data-param={param.name}>
                <ParamWidget
                  param={param}
                  value={values[param.name] ?? (param.initial as ParamValue)}
                  slug={slug}
                  version={version}
                  fonts={fonts}
                  sampleText={sampleText}
                  extruder={extruderOf(param.name)}
                  onChange={(next) => onChange(param.name, next)}
                />
              </li>
            ))}
          </ul>
        )}
      </div>

      <footer className="flex shrink-0 items-center justify-between gap-3 border-t border-line px-3 py-2">
        <span className="text-[12px] text-faint">
          {isDefault ? 'Defaults' : `${changed} changed from defaults`}
        </span>
        <Button size="sm" onClick={onReset} disabled={isDefault}>
          Reset to defaults
        </Button>
      </footer>
    </section>
  )
}
