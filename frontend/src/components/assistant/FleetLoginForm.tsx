import { useState, type FormEvent } from 'react'
import { ChevronDownIcon } from 'lucide-react'
import { USER_ONLY } from '../../agent/dom'
import { AI_CREDENTIAL_NAME_MAX } from '../../api/aiCredential'
import { ApiError } from '../../api/client'
import { addFleetLogin, loginObject } from '../../api/fleetLogin'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Spinner } from '../ui/spinner'

/**
 * Adds a Claude login for the fleet's machines (#7984), through core's person route: the
 * assistant never uses it, and only a machine that leases it reads its secret. The pasted
 * login never leaves this form but in the one request, and the assistant cannot read the form.
 */
export function FleetLoginForm({ onAdded }: { onAdded: () => void }) {
  const [open, setOpen] = useState(false)
  const [pasted, setPasted] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  async function add(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setNotice(null)
    const secret = loginObject(pasted.trim())
    if (secret === null) {
      setError('Paste the whole .credentials.json file, or the claudeAiOauth object in it.')
      return
    }
    setBusy(true)
    try {
      const { credential } = await addFleetLogin({
        kind: 'claude_login',
        secret,
        ...(name.trim() ? { name: name.trim() } : {}),
      })
      setPasted('')
      setName('')
      setNotice(`Added ${credential.name ?? 'the login'} for the fleet. A machine leases it when its own login hits a limit.`)
      onAdded()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not add the login.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Collapsible open={open} onOpenChange={setOpen} {...USER_ONLY}>
      <CollapsibleTrigger asChild>
        <Button type="button" size="sm" variant="ghost" className="-ml-2 self-start">
          <ChevronDownIcon className={open ? 'rotate-180 transition-transform' : 'transition-transform'} />
          Add a Claude login for the fleet
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <form className="mt-2 flex flex-col gap-3" onSubmit={(event) => void add(event)} data-testid="fleet-login-form">
          <p className="text-xs text-muted-foreground">
            For the fleet&rsquo;s machines, not the assistant. On a machine signed in with <code>/login</code>, copy{' '}
            <code>~/.claude/.credentials.json</code> and paste it here. When a machine&rsquo;s login hits a usage limit,
            it moves to the next free login.
          </p>
          <div className="flex flex-col gap-1">
            <Label htmlFor="fleet-login-name" className="text-[13px] font-normal">
              Name (optional)
            </Label>
            <Input
              id="fleet-login-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={AI_CREDENTIAL_NAME_MAX}
              placeholder="e.g. spark's Max login"
              className="max-w-md"
              autoComplete="off"
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="fleet-login-json" className="text-[13px] font-normal">
              Login file
            </Label>
            <Textarea
              id="fleet-login-json"
              required
              value={pasted}
              onChange={(event) => setPasted(event.target.value)}
              placeholder='{"claudeAiOauth": {"accessToken": "sk-ant-oat01-…", …}}'
              className="max-w-md font-mono text-xs [-webkit-text-security:disc]"
              rows={4}
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            Stored encrypted. Only a machine that leases it reads it; the list shows its last four characters.
          </p>
          <div>
            <Button type="submit" disabled={busy || pasted.trim() === ''} aria-busy={busy}>
              {busy && <Spinner />}
              Add login
            </Button>
          </div>
          {notice && (
            <p role="status" className="text-xs text-ok">
              {notice}
            </p>
          )}
          {error && (
            <p role="alert" className="text-xs text-warn">
              {error}
            </p>
          )}
        </form>
      </CollapsibleContent>
    </Collapsible>
  )
}
