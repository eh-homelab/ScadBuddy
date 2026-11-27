import QRCode from 'qrcode'
import { SmartphoneIcon } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { createPairing, onThisHost, pairLink, type Device, type Pairing } from '../../api/devices'
import { Spinner } from '../ui/spinner'
import { Button } from '@/components/ui/button'

const timeOf = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

/**
 * #7905 — "Add a device" (plan Task 9): posts `POST /devices/pairings` and shows its code as
 * `scadbuddy://pair?url=<encoded>`, as a QR code to scan with the phone and as a link to tap on
 * the phone itself. The code works once and for 10 minutes. `devices` is the list the parent
 * reads again; a device in it that was not there when the code was made is the one that paired.
 *
 * The link holds a one-time pairing code, a credential: the button and the link are
 * `USER_ONLY`, so the browser agent can neither make a code nor read one.
 */
export function AddDevice({ devices, onPairing }: { devices: readonly Device[] | undefined; onPairing?: (open: boolean) => void }) {
  const [pairing, setPairing] = useState<{ link: string; expiresAt: string; svg: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [paired, setPaired] = useState<Device | null>(null)
  const known = useRef<Set<string> | null>(null)
  const latestOnPairing = useRef(onPairing)
  useEffect(() => {
    latestOnPairing.current = onPairing
  })

  async function start() {
    setBusy(true)
    setError(null)
    setPaired(null)
    try {
      const made: Pairing = await createPairing()
      const link = pairLink(onThisHost(made.code_url))
      const svg = await QRCode.toString(link, { type: 'svg', errorCorrectionLevel: 'M', margin: 0 })
      known.current = new Set((devices ?? []).map((one) => one.id))
      setPairing({ link, expiresAt: made.expires_at, svg })
      latestOnPairing.current?.(true)
    } catch (cause) {
      setError(`Could not make a pairing code: ${cause instanceof Error ? cause.message : String(cause)}`)
    } finally {
      setBusy(false)
    }
  }

  const close = useCallback(() => {
    setPairing(null)
    known.current = null
    latestOnPairing.current?.(false)
  }, [])

  // The code is spent once a new device shows in the list.
  useEffect(() => {
    if (!pairing || !known.current || !devices) return
    const fresh = devices.find((one) => !known.current?.has(one.id))
    if (!fresh) return
    setPaired(fresh)
    close()
  }, [pairing, devices, close])

  // Past its expiry the code pairs nothing: take it off the screen.
  useEffect(() => {
    if (!pairing) return
    const timer = setTimeout(() => {
      close()
      setError('The pairing code expired. Make a new one.')
    }, Math.min(Math.max(0, new Date(pairing.expiresAt).getTime() - Date.now()), 2 ** 31 - 1))
    return () => clearTimeout(timer)
  }, [pairing, close])

  if (!pairing) {
    return (
      <div className="flex flex-col gap-2">
        <div>
          <Button type="button" onClick={() => void start()} disabled={busy || devices === undefined} aria-busy={busy} {...USER_ONLY}>
            {busy ? <Spinner /> : <SmartphoneIcon aria-hidden="true" />} Add a device
          </Button>
        </div>
        {paired && (
          <p role="status" className="text-ok">
            Paired {paired.name}.
          </p>
        )}
        {error && (
          <p role="alert" className="text-warn">
            {error}
          </p>
        )}
      </div>
    )
  }

  return (
    <section aria-label="Pair a device" className="flex flex-col gap-4 rounded-lg border border-line bg-surface p-4 sm:flex-row sm:items-start" {...USER_ONLY}>
      <div
        role="img"
        aria-label="Pairing QR code"
        className="size-44 shrink-0 self-center rounded-md bg-white p-3 sm:self-start [&>svg]:block [&>svg]:size-full"
        dangerouslySetInnerHTML={{ __html: pairing.svg }}
      />
      <div className="flex min-w-0 flex-col gap-2">
        <p className="font-medium">Scan this code with the phone’s camera</p>
        <ol className="list-decimal space-y-1 pl-5 text-muted-foreground">
          <li>Install the ScadBuddy app on the phone.</li>
          <li>Scan the code. The app asks you to pair with this address.</li>
          <li>Tap Pair. The phone then shows in the list below.</li>
        </ol>
        <p>
          On the phone itself?{' '}
          <a href={pairing.link} className="font-medium text-primary underline underline-offset-4">
            Open the pairing link
          </a>
        </p>
        <p className="text-muted-foreground">The code works once, until {timeOf(pairing.expiresAt)}.</p>
        <div>
          <Button type="button" variant="outline" size="sm" onClick={close}>
            Cancel
          </Button>
        </div>
      </div>
    </section>
  )
}
