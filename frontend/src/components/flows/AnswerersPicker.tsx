import { Checkbox } from '@/components/ui/checkbox'
import { api } from '../../api/client'
import { answerersOrder } from '../../lib/flows'
import { useAsync } from '../../lib/useAsync'

const RULES = [
  { rule: 'starter', label: 'The session that started the run' },
  { rule: 'sessions', label: 'Any of your assistant sessions' },
] as const

const TOKEN = 'token:'

/**
 * #7471 — who besides a person may answer a flow's questions: the session that started the
 * run, any of the person's sessions, or named MCP tokens. The setting `flow_answerers` and a
 * run grant's `answers` both use it. A token that is revoked or gone stays shown while the
 * list holds it, so it can be removed. Without `starter` (a run a person starts), the starting
 * session is not offered: no session matches it there.
 */
export function AnswerersPicker({
  id,
  rules,
  onChange,
  label = 'Who else may answer',
  empty = 'People only: nothing is chosen.',
  starter = true,
}: {
  id: string
  rules: readonly string[]
  onChange: (rules: string[]) => void
  label?: string
  empty?: string
  starter?: boolean
}) {
  const tokens = useAsync(() => api.listMcpTokens(), [])
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
    onChange(answerersOrder(checked ? [...rules, rule] : rules.filter((one) => one !== rule)))
  }

  return (
    <fieldset id={id} aria-label={label} className="flex flex-col gap-1.5">
      <span className="text-xs text-muted-foreground">
        {rules.length === 0 ? empty : "A question the flow asks of people only is still a person's."}
      </span>
      {RULES.filter(({ rule }) => starter || rule !== 'starter').map(({ rule, label: text }) => (
        <label key={rule} className="flex items-center gap-2 text-sm">
          <Checkbox aria-label={text} checked={rules.includes(rule)} onCheckedChange={(c) => toggle(rule, c === true)} />
          <span>{text}</span>
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
  )
}
