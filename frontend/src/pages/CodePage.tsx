import { useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate, useParams } from 'react-router'
import { api, ApiError } from '../api/client'
import { resolveCode } from '../api/filament'
import type { TaskQuestion } from '../api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { TaskPanel } from '../components/flows/TaskPanel'
import { inputFields } from '../lib/flowInputs'
import { flowRunPath } from '../lib/flows'
import { misfit } from '../lib/scanMisfit'
import { useAsync } from '../lib/useAsync'

/** #6955 — `/x/<code>`: the address a label's QR holds. It opens the place or spool the code names. #8062: an open task that waits for the code comes first. */
export function CodePage() {
  const { code = '' } = useParams()
  const navigate = useNavigate()
  const [typed, setTyped] = useState('')
  const found = useAsync(() => resolveCode(code), [code])

  // A failed read is logged and leaves today's navigation.
  const waiting = useAsync(
    () =>
      api.listTasks({ code }).catch((cause: unknown) => {
        console.warn('code page: could not read the tasks that wait for the code', cause)
        return [] as TaskQuestion[]
      }),
    [code],
  )

  const [done, setDone] = useState<'sent' | 'elsewhere' | null>(null)

  const to = found.data ? (found.data.kind === 'location' ? `/filament/locations/${found.data.id}` : `/filament/spools/${found.data.id}`) : null
  const tasks = waiting.data ?? []
  // Both reads first: a task that waits for the code comes before the code's own page.
  if (found.loading || waiting.loading) return <Skeleton role="status" aria-label="Looking up the code" className="m-4 h-8 w-48" />
  if (tasks.length > 0) {
    const open = (word: string) => to && <Link to={to} className="text-sm underline">{`Open ${code}${word}`}</Link>
    const instead = open(' instead')
    // The field the code fits (the server's rule); the first scan field when the code did not resolve or fits none.
    const fields = tasks.length === 1 ? inputFields(tasks[0]!.schema).filter((field) => field.widget === 'scan') : []
    const scan = (found.data && fields.find((field) => misfit(found.data!, field.expects) === null)) ?? fields[0]
    if (done) {
      return (
        <div className="mx-auto flex w-full max-w-md flex-col gap-4 p-4">
          <h1 role="status" className="text-lg font-semibold">{done === 'elsewhere' ? 'This task was answered elsewhere.' : 'Answered.'}</h1>
          {open('')}
        </div>
      )
    }
    return (
      <div className="mx-auto flex w-full max-w-md flex-col gap-4 p-4">
        {tasks.length === 1 ? (
          <Card size="sm" role="region" aria-label={tasks[0]!.title}>
            <CardContent className="flex flex-col gap-2 text-base">
              <h1 className="text-lg font-semibold break-words">{tasks[0]!.title}</h1>
              <TaskPanel
                task={tasks[0]!}
                open
                respond={async (send) => {
                  await send()
                }}
                onClosed={(by) => setDone(by)}
                onSettled={() => undefined}
                offer={scan ? { field: scan.name, code: found.data?.code ?? code, n: 1 } : undefined}
              />
            </CardContent>
          </Card>
        ) : (
          <>
            <h1 className="text-lg font-semibold">{`${tasks.length} tasks wait for ${code}`}</h1>
            <ul className="flex flex-col gap-2">
              {tasks.map((task) => (
                <li key={task.id}>
                  <Link to={flowRunPath(task.run_id)} className="text-base underline">
                    {task.title}
                  </Link>
                </li>
              ))}
            </ul>
          </>
        )}
        {instead}
      </div>
    )
  }
  if (to) return <Navigate to={to} replace />

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
