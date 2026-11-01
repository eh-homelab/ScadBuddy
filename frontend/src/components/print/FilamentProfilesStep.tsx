import { Checkbox } from '@/components/ui/checkbox'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { StepCard } from './StepCard'
import type { ProfileSuggestion } from '../../lib/filamentProfiles'

type Change = NonNullable<ProfileSuggestion['preview']>['changes'] extends (infer C)[] | undefined ? C : never

/** "200 / 500": each distinct value the preset has, per extruder variant. */
function current(change: Change): string | null {
  const value = change.current
  if (value == null) return null
  const values = Array.isArray(value) ? [...new Set(value)] : [value]
  return values.join(' / ')
}

/** Every page the profile cites, for its settings and its notes: one link a page, with
 * each passage quoted from it. */
function sourcesOf(profile: ProfileSuggestion['profile']): { url: string; title: string; quotes: string[] }[] {
  const pages = new Map<string, { url: string; title: string; quotes: string[] }>()
  const entries = [...profile.settings, ...(profile.filament_settings ?? []), ...(profile.notes ?? [])]
  for (const source of entries.flatMap((entry) => entry.sources ?? [])) {
    const page = pages.get(source.url) ?? { url: source.url, title: source.title, quotes: [] }
    if (!page.quotes.includes(source.quote)) page.quotes.push(source.quote)
    pages.set(source.url, page)
  }
  return [...pages.values()]
}

function slotsOf(suggestion: ProfileSuggestion): string {
  return suggestion.slots
    .map(({ slot_id, spool }) => `slot ${slot_id} (${[spool.brand, spool.material, spool.subtype].filter(Boolean).join(' ')})`)
    .join(', ')
}

/**
 * #717 — the filament profiles this print's spools match: each suggested and on, its
 * changes shown against the process preset's value, its source a link away, and a
 * switch to print without it. A profile that cannot apply says why and stays off; the
 * print is never refused for it.
 */
export function FilamentProfilesStep({
  suggestions,
  onToggle,
}: {
  suggestions: ProfileSuggestion[]
  onToggle: (id: string, on: boolean) => void
}) {
  if (suggestions.length === 0) return null
  return (
    <StepCard title="Filament profiles" testId="filament-profiles">
      <ul className="space-y-3">
        {suggestions.map((suggestion) => {
          const { profile, preview } = suggestion
          const id = `filament-profile-${profile.id}`
          const unapplicable = preview?.applicable === false
          const sources = sourcesOf(profile)
          return (
            <li key={profile.id} className="text-xs" data-testid={id}>
              <label htmlFor={id} className="flex cursor-pointer items-start gap-2">
                <Checkbox
                  id={id}
                  className="mt-0.5"
                  checked={suggestion.on}
                  disabled={unapplicable}
                  onCheckedChange={(checked) => onToggle(profile.id, checked === true)}
                />
                <span>
                  <span className="text-sm">{profile.name}</span>
                  <span className="text-muted-foreground"> for {slotsOf(suggestion)}</span>
                </span>
              </label>
              <p className="mt-0.5 ml-6 text-muted-foreground">{profile.description}</p>
              {unapplicable ? (
                <p className="mt-0.5 ml-6 text-destructive" role="status">
                  Not applied: {preview.reason}. The print uses the preset as it is.
                </p>
              ) : (
                <ul className="sb-num mt-0.5 ml-6" aria-label={`${profile.name} changes`}>
                  {(preview?.changes?.length ? preview.changes : profile.settings).map((change) => {
                    const from = 'current' in change && !suggestion.unchecked ? current(change) : null
                    return (
                      <li key={change.key}>
                        {change.label}: {from !== null ? `${from} → ` : '→ '}
                        {change.value}
                        {change.unit ? ` ${change.unit}` : ''}
                      </li>
                    )
                  })}
                  {/* #2799: set in the matching slots' filament presets, by the native slicer only. */}
                  {(profile.filament_settings ?? []).map((setting) => (
                    <li key={setting.key}>
                      {setting.label} (the spool's filament): → {setting.value}
                      {setting.unit ? ` ${setting.unit}` : ''}
                    </li>
                  ))}
                </ul>
              )}
              {!unapplicable && preview?.filament_applied === false && (profile.filament_settings ?? []).length > 0 && (
                <p className="mt-0.5 ml-6 text-muted-foreground" data-testid={`${id}-filament-not-applied`}>
                  {(profile.filament_settings ?? []).map((setting) => setting.label).join(' and ')} not applied:{' '}
                  {preview.filament_reason}.
                </p>
              )}
              {suggestion.unchecked && (
                <p className="mt-0.5 ml-6 text-muted-foreground">
                  Couldn't read the preset, so this wasn't checked against this print. If it can't apply, the print
                  goes ahead without it and says so.
                </p>
              )}
              {(profile.notes ?? []).map((note) => (
                <p key={note.text} className="mt-0.5 ml-6 text-muted-foreground">
                  {note.text}
                </p>
              ))}
              {sources.map((source) => (
                <Tooltip key={source.url}>
                  <TooltipTrigger asChild>
                    <a
                      href={source.url}
                      target="_blank"
                      rel="noreferrer"
                      className="mt-0.5 ml-6 block w-fit text-primary underline-offset-4 hover:underline"
                    >
                      Why: {source.title}
                    </a>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-sm whitespace-pre-line">
                    {source.quotes.map((quote) => `“${quote}”`).join('\n')}
                  </TooltipContent>
                </Tooltip>
              ))}
            </li>
          )
        })}
      </ul>
    </StepCard>
  )
}
