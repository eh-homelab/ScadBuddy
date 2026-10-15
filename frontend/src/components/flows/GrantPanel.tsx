import { useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { FlowGrantRule, FlowGrantSpec, FlowGrantView } from '../../api/types'
import { USER_ONLY } from '../../agent/dom'
import { actorText } from '../../lib/flows'
import { grantStatus, isUnbounded, unboundedText } from '../../lib/grants'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Confirm } from './Confirm'
import { GrantEditor } from './GrantEditor'

const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : '—')
const grams = (g: number) => String(Math.round(g * 10) / 10)

function bounds(rule: FlowGrantRule): string[] {
  const out: string[] = []
  if (rule.printers) out.push(rule.printers.length ? `printers ${rule.printers.join(', ')}` : 'no printer')
  if (rule.models) out.push(rule.models.length ? `models ${rule.models.join(', ')}` : 'no model')
  if (rule.library_files) out.push(rule.library_files.length ? `library files ${rule.library_files.join(', ')}` : 'no library file')
  for (const arg of rule.args ?? []) out.push(`${arg.path} in ${arg.in.map((v) => JSON.stringify(v)).join(', ')}`)
  return out
}

/** Each figure the rule counts, against its maximum when it sets one. */
function usage(rule: FlowGrantRule, used: FlowGrantView['used'][string] | undefined): string[] {
  const prints = used?.prints ?? 0
  const filament = used?.filament_g ?? 0
  const calls = used?.calls ?? 0
  const out: string[] = []
  if (rule.fn === 'queue_print') {
    out.push(rule.max_prints != null ? `${prints} / ${rule.max_prints} prints` : `${prints} prints`)
    out.push(rule.max_filament_g != null ? `${grams(filament)} / ${grams(rule.max_filament_g)} g` : `${grams(filament)} g`)
  }
  out.push(rule.max_calls != null ? `${calls} / ${rule.max_calls} calls` : `${calls} calls`)
  return out
}

interface Props {
  runId: string
  grant: FlowGrantView
  /** After a decision: the page reads the run again. */
  onChange: () => void
}

/**
 * #2481 (spec 2026-10-11 §3.2, §3.5) — a run's grant: its state, what each rule let
 * through against its maxima, and every call it let through. A requested grant (an
 * agent's proposal) is approved here, as asked or edited in the editor, or declined; an
 * approved one is revoked, after a confirmation. Each is final, and a person's only: the
 * routes refuse an agent, and the buttons are user-only.
 */
export function GrantPanel({ runId, grant, onChange }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const [approving, setApproving] = useState(false)
  const [revoking, setRevoking] = useState(false)
  const rules = grant.spec.rules ?? []
  const live = grant.status === 'approved' && !grant.revoked_at

  async function act(send: () => Promise<unknown>): Promise<boolean> {
    setBusy(true)
    setError(undefined)
    try {
      await send()
      onChange()
      return true
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
      return false
    } finally {
      setBusy(false)
    }
  }

  async function approve(spec: FlowGrantSpec) {
    if (await act(() => api.approveFlowGrant(runId, spec))) setApproving(false)
  }

  async function revoke() {
    if (await act(() => api.revokeFlowGrant(runId))) setRevoking(false)
  }

  return (
    <Card size="sm" role="region" aria-label="Grant" className="mb-3 text-[13px]">
      <CardHeader className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <CardTitle>
          <h2>Grant</h2>
        </CardTitle>
        <p className="flex items-center gap-1.5">
          <span className="text-muted-foreground">Status</span>
          <Badge variant={live ? 'default' : 'secondary'} data-testid="grant-status">
            {grantStatus(grant)}
          </Badge>
        </p>
        <CardDescription>
          {grant.status === 'requested'
            ? `Requested by ${actorText(grant.requested_by)}`
            : grant.revoked_at
              ? `Revoked by ${grant.revoked_by ?? 'someone'}, ${when(grant.revoked_at)}`
              : grant.approved_at
                ? `Approved by ${grant.approved_by ?? 'someone'}, ${when(grant.approved_at)}`
                : null}
        </CardDescription>
        <CardDescription>Expires {when(grant.expires_at ?? grant.spec.expires_at)}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <p className="text-[12.5px] text-muted-foreground">
          {grant.spec.sessions ? 'Extends to the sessions it starts.' : 'Does not extend to the sessions it starts.'}{' '}
          {grant.spec.ask_starter_session
            ? 'Calls outside it ask the starting session first.'
            : 'Calls outside it come to you.'}
        </p>
        {rules.length === 0 ? (
          <p className="text-[12.5px] text-muted-foreground">No rules: every outward call asks.</p>
        ) : (
          <table aria-label="Grant rules" className="w-full text-left text-[12.5px]">
            <thead className="text-muted-foreground">
              <tr>
                <th className="py-1 font-normal">#</th>
                <th className="py-1 font-normal">Rule</th>
                <th className="py-1 font-normal">Bounds</th>
                <th className="py-1 font-normal">Used</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((rule, i) => (
                <tr key={i} className="border-t align-top">
                  <td className="py-1">{i + 1}</td>
                  <td className="py-1 font-mono">{rule.fn === 'tool' ? `tool: ${rule.tool ?? ''}` : rule.fn}</td>
                  <td className="py-1">
                    {isUnbounded(rule) ? (
                      <span className="text-destructive">{unboundedText(rule)}</span>
                    ) : (
                      bounds(rule).map((b) => <span key={b} className="block">{b}</span>)
                    )}
                  </td>
                  <td className="py-1">
                    {usage(rule, grant.used[String(i)]).map((u) => (
                      <span key={u} className="block">
                        {u}
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {grant.uses.length > 0 && (
          <table aria-label="Grant uses" className="w-full text-left text-[12.5px]">
            <thead className="text-muted-foreground">
              <tr>
                <th className="py-1 font-normal">Time</th>
                <th className="py-1 font-normal">Call</th>
                <th className="py-1 font-normal">Tool</th>
                <th className="py-1 font-normal">Session</th>
              </tr>
            </thead>
            <tbody>
              {grant.uses.map((use) => (
                <tr key={use.request_id} className="border-t">
                  <td className="py-1">{when(use.created_at)}</td>
                  <td className="py-1 font-mono">{use.fn}</td>
                  <td className="py-1 font-mono">{use.tool ?? '—'}</td>
                  <td className="py-1 font-mono">{use.session_id ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {error && !approving && !revoking && (
          <p role="alert" className="text-destructive">
            {error}
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          {grant.status === 'requested' && !grant.revoked_at && (
            <>
              <Button size="sm" onClick={() => setApproving(true)} disabled={busy} {...USER_ONLY}>
                Approve grant…
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={() => void act(() => api.declineFlowGrant(runId))}
                disabled={busy}
                {...USER_ONLY}
              >
                Decline grant
              </Button>
            </>
          )}
          {live && (
            <Confirm
              trigger={
                <Button variant="destructive" size="sm" disabled={busy} {...USER_ONLY}>
                  Revoke grant
                </Button>
              }
              open={revoking}
              onOpenChange={setRevoking}
              title="Revoke this grant?"
              action="Revoke grant"
              busy={busy}
              error={error}
              onAction={() => void revoke()}
            >
              <p>
                The run goes on, but from now on every outward call it makes waits for a decision. Revoking is final: the
                run cannot get this grant, or another, back.
              </p>
            </Confirm>
          )}
        </div>
      </CardContent>
      <Dialog open={approving} onOpenChange={(open) => !open && !busy && setApproving(false)}>
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Approve this grant</DialogTitle>
            <DialogDescription>
              Approve it as requested, or narrow (or widen) any bound first: the request was only a proposal.
            </DialogDescription>
          </DialogHeader>
          <GrantEditor
            initial={grant.spec}
            submitLabel="Approve grant"
            busy={busy}
            onSubmit={(spec) => void approve(spec)}
            onCancel={() => setApproving(false)}
          />
          {error && (
            <p role="alert" className="text-[13px] text-destructive">
              {error}
            </p>
          )}
        </DialogContent>
      </Dialog>
    </Card>
  )
}
