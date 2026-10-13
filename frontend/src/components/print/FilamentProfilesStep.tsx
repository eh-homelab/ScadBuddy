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
  for (const source of [...profile.settings, ...(profile.notes ?? [])].flatMap((entry) => entry.sources ?? [])) {
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
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2" data-testid="filament-profiles">
      <legend className="px-1 text-[13px] text-ink">Filament profiles</legend>
      <ul className="mt-1 space-y-2">
        {suggestions.map((suggestion) => {
          const { profile, preview } = suggestion
          const id = `filament-profile-${profile.id}`
          const unapplicable = preview?.applicable === false
          const sources = sourcesOf(profile)
          return (
            <li key={profile.id} className="text-[12px]" data-testid={id}>
              <label htmlFor={id} className="flex items-start gap-2 text-ink">
                <input
                  id={id}
                  type="checkbox"
                  className="mt-0.5"
                  checked={suggestion.on}
                  disabled={unapplicable}
                  onChange={(event) => onToggle(profile.id, event.target.checked)}
                />
                <span>
                  <span className="text-[12.5px]">{profile.name}</span>
                  <span className="text-muted"> for {slotsOf(suggestion)}</span>
                </span>
              </label>
              <p className="mt-0.5 ml-5 text-muted">{profile.description}</p>
              {unapplicable ? (
                <p className="mt-0.5 ml-5 text-warn" role="status">
                  Not applied: {preview.reason}. The print uses the preset as it is.
                </p>
              ) : (
                <ul className="sb-num mt-0.5 ml-5 text-ink" aria-label={`${profile.name} changes`}>
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
                </ul>
              )}
              {suggestion.unchecked && (
                <p className="mt-0.5 ml-5 text-muted">
                  Couldn't read the preset, so this wasn't checked against this print. If it can't apply, the print
                  goes ahead without it and says so.
                </p>
              )}
              {(profile.notes ?? []).map((note) => (
                <p key={note.text} className="mt-0.5 ml-5 text-muted">
                  {note.text}
                </p>
              ))}
              {sources.map((source) => (
                <a
                  key={source.url}
                  href={source.url}
                  target="_blank"
                  rel="noreferrer"
                  title={source.quotes.map((quote) => `“${quote}”`).join('\n')}
                  className="mt-0.5 ml-5 block text-accent hover:underline"
                >
                  Why: {source.title}
                </a>
              ))}
            </li>
          )
        })}
      </ul>
    </fieldset>
  )
}
