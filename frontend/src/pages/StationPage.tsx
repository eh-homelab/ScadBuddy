import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router'
import { api, ApiError } from '../api/client'
import { getLocation, listLocations, resolveCode } from '../api/filament'
import type { TaskQuestion } from '../api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { TaskPanel } from '../components/flows/TaskPanel'
import type { ScanOffer } from '../components/flows/widgets/ScanField'
import { inputFields } from '../lib/flowInputs'
import { misfit } from '../lib/scanMisfit'
import { useAsync } from '../lib/useAsync'

/** `tasks:<place>`: the topic a place's task changes are published on. */
const placeTopic = (place: string) => `tasks:${place}`

type Offer = ScanOffer & { taskId: string }

/** A task's scan fields (the server's too: an entity field is never offered a scan) and what each expects. */
function scanFields(task: TaskQuestion) {
  return inputFields(task.schema).filter((field) => field.widget === 'scan')
}

/** The most the route returns (`MAX_TASKS`): a list this long may be cut. */
const LIST_MAX = 200

/**
 * #8062 (spec §6.3) — `/station/<place>`: the open tasks at one place, oldest first, for a
 * person standing at the bench. The scan field at the top keeps the focus. A code scanned
 * there goes to the one task whose scan field expects it. The page is an ordinary signed-in
 * session, so each answer is the signed-in person's own. `/station` lists the stations.
 */
export function StationPage() {
  const { place } = useParams()
  return place ? <Station key={place} place={place} /> : <Stations />
}

function Stations() {
  const stations = useAsync(() => listLocations({ station: true }), [])
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-4 p-4">
      <h1 className="text-2xl font-semibold">Stations</h1>
      {stations.loading && <Skeleton role="status" aria-label="Loading stations" className="h-14 w-full" />}
      {stations.error && (
        <p role="alert" className="text-sm text-destructive">
          The stations could not be loaded. Try again.
        </p>
      )}
      {stations.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No place is a station yet. Open a place in Filament, then Locations, and turn on “Station for flow tasks”.
        </p>
      )}
      <ul className="flex flex-col gap-3">
        {stations.data?.map((node) => (
          <li key={node.id}>
            <Link
              to={`/station/${encodeURIComponent(node.id)}`}
              className="flex min-h-16 items-center rounded-xl px-4 text-lg font-medium ring-1 ring-foreground/10 hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              {node.name}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}

function Station({ place }: { place: string }) {
  const where = useAsync(() => getLocation(place), [place])
  const tasks = useAsync(() => api.listTasks({ place, limit: LIST_MAX }), [place], [placeTopic(place)])
  const scan = useRef<HTMLInputElement>(null)
  const [code, setCode] = useState('')
  const [said, setSaid] = useState('')
  const [offer, setOffer] = useState<Offer | null>(null)
  /** Tasks that wait for one scan: the person picks. */
  const [choices, setChoices] = useState<{ code: string; fits: { task: TaskQuestion; field: string }[] } | null>(null)
  const count = useRef(0)
  const open = tasks.data ?? []

  // The scan field holds the focus: a task that goes (answered here or elsewhere) takes
  // the focus it held with it, and the field has it back.
  useEffect(() => {
    if (!document.activeElement || document.activeElement === document.body) scan.current?.focus()
  }, [tasks.data])

  async function scanned(event: FormEvent) {
    event.preventDefault()
    const typed = code.trim()
    if (!typed) return
    setSaid('')
    setChoices(null)
    let found
    try {
      found = await resolveCode(typed)
    } catch (caught) {
      setSaid(caught instanceof ApiError && caught.status === 404 ? `No code ${typed} is known.` : 'The code could not be looked up. Try again.')
      scan.current?.focus()
      return
    }
    // The server decides which tasks wait for the code, by the rule it answers with.
    let waiting: TaskQuestion[]
    try {
      waiting = await api.listTasks({ place, code: found.code, limit: LIST_MAX })
    } catch {
      setSaid('The tasks could not be checked. Try again.')
      scan.current?.focus()
      return
    }
    const fits = waiting.flatMap((task) => {
      const fields = scanFields(task)
      const field = fields.find((one) => misfit(found, one.expects) === null) ?? fields[0]
      return field ? [{ task, field: field.name }] : []
    })
    if (fits.length === 0) {
      setSaid(`No task here waits for ${found.code}.`)
      scan.current?.focus()
    } else if (fits.length > 1) {
      setChoices({ code: found.code, fits })
      setCode('')
    } else {
      give(fits[0]!.task.id, fits[0]!.field, found.code)
    }
  }

  function give(taskId: string, field: string, scanned: string) {
    setChoices(null)
    setOffer({ taskId, field, code: scanned, n: ++count.current })
    setCode('')
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-5 p-4">
      <h1 className="text-2xl font-semibold break-words">{where.data?.name ?? 'Station'}</h1>
      <form onSubmit={(event) => void scanned(event)} className="flex flex-col gap-2">
        <Input
          ref={scan}
          aria-label="Scan a code"
          autoFocus
          type="text"
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          placeholder="Scan a spool or place"
          value={code}
          className="h-16 font-mono text-xl md:text-xl"
          onChange={(event) => {
            setSaid('')
            setCode(event.target.value)
          }}
          // A click on bare page takes the focus to nothing: it comes back. Another field has it for a reason.
          onBlur={(event) => {
            if (!event.relatedTarget) setTimeout(() => scan.current?.focus())
          }}
        />
        <p role="alert" className="min-h-5 text-sm text-destructive">
          {said}
        </p>
        {choices && (
          <div role="group" aria-label={`Tasks that wait for ${choices.code}`} className="flex flex-col gap-2">
            <p className="text-sm">{`${choices.fits.length} tasks wait for ${choices.code}. Choose one.`}</p>
            {choices.fits.map(({ task, field }) => (
              <Button key={task.id} type="button" variant="outline" size="lg" className="h-14 justify-start text-base" onClick={() => give(task.id, field, choices.code)}>
                {task.title}
              </Button>
            ))}
          </div>
        )}
      </form>
      {tasks.loading && <Skeleton role="status" aria-label="Loading tasks" className="h-24 w-full" />}
      {tasks.error && (
        <p role="alert" className="text-sm text-destructive">
          The tasks could not be loaded. Try again.
        </p>
      )}
      {tasks.data && open.length === 0 && (
        <p className="text-base text-muted-foreground">No open tasks here. This page shows a new one as soon as it arrives.</p>
      )}
      {open.length >= LIST_MAX && <p className="text-sm text-muted-foreground">More tasks wait. Answer these first.</p>}
      {open.map((task) => (
        <Card key={task.id} size="sm" role="region" aria-label={task.title}>
          <CardContent className="flex flex-col gap-2 text-base">
            <h2 className="text-lg font-semibold break-words">{task.title}</h2>
            <TaskPanel
              task={task}
              open
              respond={async (send) => {
                await send()
              }}
              onClosed={() => undefined}
              onSettled={() => tasks.refresh()}
              offer={offer?.taskId === task.id ? offer : undefined}
            />
          </CardContent>
        </Card>
      ))}
    </div>
  )
}
