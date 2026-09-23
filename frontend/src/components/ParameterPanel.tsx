import { useMemo, useState } from 'react'
import type { FontFamily, ModelSchema, ParamValue } from '../api/types'
import { colorParamNames, diffFromDefaults, type ParamValues } from '../lib/params'
import { ParamWidget } from './widgets/ParamWidget'
import { Button } from './ui/Button'

const GLOBAL_GROUP = 'Global'

interface Props {
  schema: ModelSchema
  values: ParamValues
  fonts: FontFamily[]
  onChange: (name: string, value: ParamValue) => void
  onReset: () => void
}

export function ParameterPanel({ schema, values, fonts, onChange, onReset }: Props) {
  const tabs = useMemo(
    () => schema.groups.filter((group) => group.name !== GLOBAL_GROUP),
    [schema],
  )
  const globalGroup = schema.groups.find((group) => group.name === GLOBAL_GROUP)
  const [active, setActive] = useState(() => tabs[0]?.name ?? GLOBAL_GROUP)
  const current = tabs.find((group) => group.name === active) ?? tabs[0]

  const extruderOf = useMemo(() => {
    const order = colorParamNames(schema)
    return (name: string) => {
      const index = order.indexOf(name)
      return index === -1 ? undefined : index + 1
    }
  }, [schema])

  const changed = diffFromDefaults(schema, values).length
  const isDefault = changed === 0

  return (
    <section
      aria-label="Parameters"
      className="flex h-full min-h-0 w-full flex-col border-r border-line bg-surface"
    >
      <div
        role="tablist"
        aria-label="Parameter groups"
        className="flex shrink-0 gap-0.5 overflow-x-auto border-b border-line px-2 pt-2"
      >
        {tabs.map((group) => {
          const selected = group.name === current?.name
          return (
            <button
              key={group.name}
              role="tab"
              type="button"
              aria-selected={selected}
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

      <div className="min-h-0 flex-1 overflow-y-auto">
        {globalGroup && (
          <div className="border-b border-line bg-surface-2/40">
            <ul className="divide-y divide-line/60">
              {globalGroup.params.map((param) => (
                <li key={param.name}>
                  <ParamWidget
                    param={param}
                    value={values[param.name] ?? param.initial}
                    fonts={fonts}
                    extruder={extruderOf(param.name)}
                    onChange={(next) => onChange(param.name, next)}
                  />
                </li>
              ))}
            </ul>
          </div>
        )}

        {current && (
          <ul role="tabpanel" aria-label={current.name} className="divide-y divide-line/60">
            {current.params.map((param) => (
              <li key={param.name}>
                <ParamWidget
                  param={param}
                  value={values[param.name] ?? param.initial}
                  fonts={fonts}
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
