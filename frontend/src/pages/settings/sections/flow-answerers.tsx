import { Checkbox } from '@/components/ui/checkbox'
import { api } from '../../../api/client'
import { useAsync } from '../../../lib/useAsync'
import { FieldRow } from '../controls'
import { docOf } from '../fields'
import { SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { answerersValue, fieldsOf } from '../values'

export const id = 'flow-answerers'
export const area: AreaId = 'assistant'
export const title = "Flows' questions"
export const order = 120

const RULES = [
  { rule: 'starter', label: 'The session that started the run' },
  { rule: 'sessions', label: 'Any of your assistant sessions' },
] as const

const TOKEN = 'token:'

/**
 * #7471 — who besides a person may answer a flow's question: the session that started the
 * run, any of the person's sessions, or named MCP tokens. Empty is people only. Only a person
 * saves it (the API refuses an agent with 403 `person_only`). A token that is revoked or gone
 * stays shown while the list holds it, so it can be removed.
 */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  const { value, setField, errors } = form
  const tokens = useAsync(() => api.listMcpTokens(), [])
  const raw = value('flow_answerers')
  const rules = raw === '' ? [] : raw.split(',')
  const held = rules.filter((rule) => rule.startsWith(TOKEN)).map((rule) => rule.slice(TOKEN.length))
  const known = tokens.data?.tokens ?? []
  const listed = [
    ...known
      .filter((token) => token.status === 'active' || held.includes(token.id))
      .map((token) => ({
        id: token.id,
        name: token.status === 'active' ? token.name : `${token.name} (${token.status})`,
        readOnly: token.tier === 'read',
      })),
    ...held
      .filter((tid) => !known.some((token) => token.id === tid))
      .map((tid) => ({ id: tid, name: `Token ${tid.slice(0, 8)} (gone)`, readOnly: false })),
  ]

  function toggle(rule: string, checked: boolean) {
    setField('flow_answerers', answerersValue(checked ? [...rules, rule] : rules.filter((one) => one !== rule)))
  }

  return (
    <SavedSection form={form} id={id} title={title}>
      <FieldRow id="flow-answerers-who" {...docOf(form.settings, 'flow_answerers')} error={errors.flow_answerers}>
        <fieldset id="flow-answerers-who" aria-label="Who else may answer" className="flex flex-col gap-1.5">
          <span className="text-xs text-muted-foreground">
            {rules.length === 0
              ? 'People only: nothing is chosen.'
              : "A question the flow asks of people only is still a person's."}
          </span>
          {RULES.map(({ rule, label }) => (
            <label key={rule} className="flex items-center gap-2 text-sm">
              <Checkbox aria-label={label} checked={rules.includes(rule)} onCheckedChange={(c) => toggle(rule, c === true)} />
              <span>{label}</span>
            </label>
          ))}
          {listed.length === 0 ? (
            <span className="text-xs text-muted-foreground">
              {tokens.error ? 'Could not read the MCP tokens.' : 'No MCP tokens.'}
            </span>
          ) : (
            listed.map((token) => (
              <label key={token.id} className="flex items-start gap-2 text-sm">
                <Checkbox
                  aria-label={`MCP token ${token.name}`}
                  checked={held.includes(token.id)}
                  onCheckedChange={(c) => toggle(`${TOKEN}${token.id}`, c === true)}
                  className="mt-0.5"
                />
                <span className="flex flex-col">
                  <span>MCP token {token.name}</span>
                  {token.readOnly && <span className="text-xs text-warn">Read only: it cannot answer.</span>}
                </span>
              </label>
            ))
          )}
        </fieldset>
      </FieldRow>
    </SavedSection>
  )
}
