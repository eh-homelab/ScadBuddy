import { useMemo, useState, type ReactNode } from 'react'
import type { CustomizerSchema, FontFamily, InstalledFamily, ParamValue } from '../api/types'
import { missingFamiliesOf, type MissingFonts } from '../lib/fonts'
import { diffFromDefaults, extrudersIn, extrudersOf, groupsOf, rangeProblem, type ParamValues } from '../lib/params'
import { ParamWidget } from './widgets/ParamWidget'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

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
  /**
   * #1286 — the render was refused for font families that are not installed: flagged
   * on their fields. One object per refusal: its identity scopes a field's install error (#2215).
   */
  missingFonts?: MissingFonts
  /** One of them was installed from its field. */
  onFontInstalled?: (installed: InstalledFamily) => void
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
  missingFonts,
  onFontInstalled,
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

  const extruders = useMemo<Map<string, number | null | undefined>>(
    () =>
      rendered ? extrudersIn(schema, values, rendered.colors, rendered.params) : extrudersOf(schema, values),
    [schema, values, rendered],
  )
  const extruderOf = (name: string) => extruders.get(name)
  const missingOf = (name: string, value: ParamValue) =>
    missingFonts ? missingFamiliesOf(missingFonts, name, String(value)) : undefined

  // The font picker previews what will actually be printed, so it needs the model's
  // own text: the first string parameter, which on the keychain is the name.
  const sampleText = useMemo(() => {
    const text = (schema.parameters ?? []).find((param) => param.type === 'string')
    return text ? String(values[text.name] ?? text.initial ?? '') : ''
  }, [schema, values])

  const changed = diffFromDefaults(schema, values).length
  const isDefault = changed === 0

  const row = (param: (typeof groups)[number]['params'][number]) => (
    <li key={param.name} data-param={param.name}>
      <ParamWidget
        param={param}
        value={values[param.name] ?? (param.initial as ParamValue)}
        slug={slug}
        version={version}
        fonts={fonts}
        sampleText={sampleText}
        extruder={extruderOf(param.name)}
        missingFonts={missingOf(param.name, values[param.name] ?? (param.initial as ParamValue))}
        fontRefusal={missingFonts}
        onFontInstalled={onFontInstalled}
        onChange={(next) => onChange(param.name, next)}
      />
    </li>
  )

  return (
    <section aria-label="Parameters" className="flex h-full min-h-0 w-full flex-col border-r bg-card">
      {toolbar}
      {/* #968 — Radix's tabs: one Tab stop, the arrows, Home and End moving the selection. */}
      <Tabs value={current?.name ?? ''} onValueChange={setActive} className="flex min-h-0 flex-1 flex-col gap-0">
        {tabs.length > 0 && (
          <TabsList
            variant="line"
            aria-label="Parameter groups"
            // #942 — wrapped onto rows, never scrolled: a template with many groups (Dollhouse
            // Kit has 15) hid most of them past an edge with no scrollbar, fade or arrow.
            className="h-auto w-full shrink-0 flex-wrap justify-start gap-x-1 gap-y-1 border-b px-2 pt-1.5 pb-1.5"
          >
            {tabs.map((group) => {
              // #2444 — a tab holding a value nothing can render is marked, so the field that
              // stops the render can be found from any other tab.
              const flagged = group.params.some((param) => rangeProblem(param, values[param.name]) !== null)
              return (
                <TabsTrigger key={group.name} value={group.name} className="h-7 flex-none px-2">
                  {group.name}
                  {flagged && (
                    <>
                      <Badge
                        aria-hidden="true"
                        data-testid="tab-flagged"
                        variant="destructive"
                        className="size-2 min-w-0 rounded-full bg-destructive p-0"
                      />
                      <span className="sr-only"> (a value to fix)</span>
                    </>
                  )}
                </TabsTrigger>
              )
            })}
          </TabsList>
        )}

        <ScrollArea
          // Radix lays its content out as a table, which grows to the widest row: block keeps
          // the phone-width rule (#362) that nothing pushes the panel wider than the screen.
          className={`min-h-0 flex-1 [&_[data-slot=scroll-area-viewport]>div]:block! ${growsWithPage ? 'short:flex-none phone:flex-none' : ''}`}>
          {globalGroup && (
            <div className="border-b bg-ui-muted/40">
              <ul className="divide-y">{globalGroup.params.map(row)}</ul>
            </div>
          )}

          {tabs.map((group) => (
            // #2455 — the panel wraps the list: as the <ul>'s own role it replaced the list
            // semantics, and a screen reader lost "list, N items".
            <TabsContent key={group.name} value={group.name}>
              <ul className="divide-y">{group.params.map(row)}</ul>
            </TabsContent>
          ))}
        </ScrollArea>
      </Tabs>

      <footer className="flex shrink-0 items-center justify-between gap-3 border-t px-3 py-2">
        <span className="text-xs text-muted-foreground">
          {isDefault ? 'Defaults' : `${changed} changed from defaults`}
        </span>
        <Button type="button" variant="outline" size="sm" onClick={onReset} disabled={isDefault}>
          Reset to defaults
        </Button>
      </footer>
    </section>
  )
}
