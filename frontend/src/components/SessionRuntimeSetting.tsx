import { useId, useState, type FormEvent } from 'react'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { api, ApiError } from '../api/client'
import { oneOf, SYSTEM_PROMPT_APPEND_MAX, SYSTEM_PROMPT_BASES, TOOL_SEARCH_MODES } from '../api/sessionTurn'
import type { SessionRuntimeUpdate } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { ChoiceSelect, LabeledInput, SaveFooter, SettingBody, SettingCard } from './settings/form'

type NumericKey = 'park_hold_seconds' | 'warm_max_per_replica' | 'warm_idle_seconds' | 'turn_max_seconds'
type TextKey = 'tool_search' | 'system_prompt_base' | 'system_prompt_append'

/** agent `sessions/runtimeSettings.ts` RUNTIME_BOUNDS (unified runtime spec §13). */
const FIELDS: { key: NumericKey; label: string; min: number; max: number }[] = [
  { key: 'park_hold_seconds', label: 'Park hold (seconds)', min: 30, max: 3600 },
  { key: 'warm_max_per_replica', label: 'Warm processes per replica', min: 0, max: 32 },
  { key: 'warm_idle_seconds', label: 'Warm process idle time (seconds)', min: 60, max: 86400 },
  { key: 'turn_max_seconds', label: 'Longest turn (seconds)', min: 300, max: 86400 },
]

/** agent `harness/toolSearch.ts` ToolSearchMode (api/sessionTurn.ts TOOL_SEARCH_MODES). */
const TOOL_SEARCH: { value: (typeof TOOL_SEARCH_MODES)[number]; label: string }[] = [
  { value: 'off', label: 'Off: send every tool on every request' },
  { value: 'auto', label: 'Auto: defer tools once they fill 10% of the context' },
  { value: 'always', label: 'Always defer tools' },
]

/** agent `sessions/turnSettings.ts` SystemPromptBase (api/sessionTurn.ts SYSTEM_PROMPT_BASES). */
const PROMPT_BASES: { value: (typeof SYSTEM_PROMPT_BASES)[number]; label: string }[] = [
  { value: 'claude_code', label: "Claude Code's system prompt" },
  { value: 'minimal', label: "The Agent SDK's minimal prompt" },
]

/**
 * The session runtime settings, stored by the agent service
 * (`/api/v1/ai/settings/session-runtime`): how long a parked call is held, how many
 * warm assistant processes a replica keeps (0 starts every turn cold) and for how
 * long, the longest a turn may run, and what each turn's Claude Code is given: tool
 * search and the system prompt. The warm cache applies a change to its size and idle time
 * at once; tool search and the system prompt take effect at each session's next turn.
 *
 * Hidden when the agent service or its database is not there. Save is user-only,
 * like the limits beside it (AI design spec §8.1).
 */
export function SessionRuntimeSetting() {
  const runtime = useAsync(() => api.getSessionRuntime(), [])
  const [draft, setDraft] = useState<Partial<Record<NumericKey | TextKey, string>>>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const searchId = useId()
  const baseId = useId()
  const appendId = useId()

  if (runtime.error || !runtime.data) return null
  const current = runtime.data
  const text = (key: NumericKey | TextKey) => draft[key] ?? String(current[key])
  const changes: SessionRuntimeUpdate = {}
  for (const { key } of FIELDS) if (Number(text(key)) !== current[key]) changes[key] = Number(text(key))
  const search = oneOf(TOOL_SEARCH_MODES, text('tool_search'))
  if (search && search !== current.tool_search) changes.tool_search = search
  const base = oneOf(SYSTEM_PROMPT_BASES, text('system_prompt_base'))
  if (base && base !== current.system_prompt_base) changes.system_prompt_base = base
  if (text('system_prompt_append') !== current.system_prompt_append) changes.system_prompt_append = text('system_prompt_append')
  const changed = Object.keys(changes).length > 0
  const set = (key: NumericKey | TextKey, value: string) => setDraft((d) => ({ ...d, [key]: value }))

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      runtime.setData(await api.putSessionRuntime(changes))
      setDraft({})
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the runtime settings')
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingCard title="Assistant session runtime">
      <form className="contents" onSubmit={(event) => void save(event)}>
        <SettingBody className="space-y-3">
          <div className="flex flex-wrap gap-4">
            {FIELDS.map(({ key, label, min, max }) => (
              <LabeledInput
                key={key}
                label={label}
                type="number"
                inputMode="numeric"
                min={min}
                max={max}
                step={1}
                required
                value={text(key)}
                onChange={(event) => set(key, event.target.value)}
                className="w-48"
                aria-describedby="session-runtime-help"
              />
            ))}
          </div>
          <p id="session-runtime-help" className="text-xs text-muted-foreground">
            A replica keeps up to this many assistant processes warm between turns, each for the idle time given; 0
            starts every turn cold. A change applies to the warm processes at once; a running turn is never stopped by it.
            A turn waiting for your answer keeps its process for the park hold, then lets it go and goes on once you
            answer. A turn that runs past the longest turn ends there.
          </p>
          <div className="grid gap-1.5">
            <Label htmlFor={searchId} className="font-normal">
              Tool search
            </Label>
            <ChoiceSelect
              id={searchId}
              value={text('tool_search')}
              onValueChange={(value) => set('tool_search', value)}
              choices={TOOL_SEARCH}
              className="sm:w-96"
              aria-describedby={`${searchId}-help`}
            />
            <p id={`${searchId}-help`} className="text-xs text-muted-foreground">
              With tool search the assistant gets a few common tools in full and the rest by name, and loads a tool when it
              needs it. Each request is smaller, so a turn costs less.
            </p>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor={baseId} className="font-normal">
              System prompt
            </Label>
            <ChoiceSelect
              id={baseId}
              value={text('system_prompt_base')}
              onValueChange={(value) => set('system_prompt_base', value)}
              choices={PROMPT_BASES}
              className="sm:w-96"
              aria-describedby={`${baseId}-help`}
            />
            <p id={`${baseId}-help`} className="text-xs text-muted-foreground">
              Claude Code&apos;s prompt has its guidance on tools, safety and coding. The minimal prompt is much smaller and
              leaves that guidance out.
            </p>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor={appendId} className="font-normal">
              Your instructions
            </Label>
            <Textarea
              id={appendId}
              value={text('system_prompt_append')}
              onChange={(event) => set('system_prompt_append', event.target.value)}
              maxLength={SYSTEM_PROMPT_APPEND_MAX}
              rows={4}
              aria-describedby={`${appendId}-help`}
            />
            <p id={`${appendId}-help`} className="text-xs text-muted-foreground">
              Added after ScadBuddy&apos;s own part, which always goes in whatever you write here: the rule that tool
              results are data and never instructions, and the note on which browser each browser tool drives. Each
              session also gets a note on the built-in tools the session itself provides, such as WebFetch and ScheduleWakeup.
            </p>
            <Collapsible className="text-xs text-muted-foreground">
              <CollapsibleTrigger asChild>
                <Button type="button" variant="link" size="sm" className="px-0">
                  ScadBuddy&apos;s part (always included)
                </Button>
              </CollapsibleTrigger>
              <CollapsibleContent>
                <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded border p-2">
                  {current.system_prompt_fixed}
                </pre>
              </CollapsibleContent>
            </Collapsible>
          </div>
          <p className="text-xs text-muted-foreground">
            A change to tool search or the system prompt starts each session&apos;s assistant process again at its next turn.
          </p>
        </SettingBody>
        <SaveFooter saving={saving} disabled={!changed} saved={saved && !changed ? 'Saved.' : null} error={error} />
      </form>
    </SettingCard>
  )
}
