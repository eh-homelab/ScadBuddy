import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate, useParams } from 'react-router'
import { ApiError } from '../api/client'
import { resolveCode } from '../api/filament'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { codeTarget } from '../lib/resolvedCode'
import { useAsync } from '../lib/useAsync'

/** #6955 — `/x/<code>`: the address a label's QR holds. It opens the place, spool, spool type, item or item kind the code names. */
export function CodePage() {
  const { code = '' } = useParams()
  const navigate = useNavigate()
  const [typed, setTyped] = useState('')
  const found = useAsync(() => resolveCode(code), [code])

  if (found.data) return <Navigate to={codeTarget(found.data)} replace />
  if (found.loading) return <Skeleton role="status" aria-label="Looking up the code" className="m-4 h-8 w-48" />

  const unknown = found.error instanceof ApiError && found.error.status === 404
  const go = (event: FormEvent) => {
    event.preventDefault()
    const next = typed.trim()
    if (next) void navigate(`/x/${encodeURIComponent(next)}`)
  }
  return (
    <div className="mx-auto flex w-full max-w-md flex-col gap-4 p-4">
      <h1 className="text-lg font-semibold">{unknown ? `No such code: ${code}` : `Could not look up ${code}`}</h1>
      {found.error && !unknown && <p role="alert" className="text-sm text-destructive">{found.error.message}</p>}
      <form onSubmit={go} className="flex gap-2">
        <Input aria-label="Code" placeholder="L-12 or S-142" value={typed} onChange={(event) => setTyped(event.target.value)} autoCapitalize="characters" autoComplete="off" />
        <Button type="submit">Open</Button>
      </form>
    </div>
  )
}
