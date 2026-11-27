import { NfcIcon, ScanQrCodeIcon } from 'lucide-react'
import { useId, useRef, useState } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../api/client'
import { codeScanFromDevice, tagReadFromDevice, type DeviceMethod, type ScannedCode } from '../../api/devices'
import type { TagReadResult } from '../../api/filament'
import { filamentLinks } from '../../lib/filamentLinks'
import { codeLabel, codeTarget } from '../../lib/resolvedCode'
import { AddDevice } from './AddDevice'
import { DeviceCall, type CallEnd } from './DeviceCall'
import { LIST_POLL_MS, PAIRING_POLL_MS, usePolledDevices } from './useDevices'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/** The methods this block offers, with their button. */
const OFFERED: { method: DeviceMethod; label: string; icon: typeof NfcIcon }[] = [
  { method: 'nfc.read', label: 'Read a tag', icon: NfcIcon },
  { method: 'code.scan', label: 'Scan a code', icon: ScanQrCodeIcon },
]

type Decoded = { kind: 'tag'; result: TagReadResult } | { kind: 'codes'; codes: ScannedCode[]; error: string | null } | { kind: 'failed'; message: string }

/** Why the code decoder read nothing, by its `error` (#8155). */
const SCAN_ERROR: Record<string, string> = {
  image_unreadable: 'ScadBuddy could not open the photo as an image. Try again.',
  image_too_large: 'The photo is too large to decode. Try again.',
}

interface Run {
  key: number
  deviceId: string
  method: DeviceMethod
}

/**
 * #7905 — "Use my phone" on the Scan page (plan Task 9): while a device of the person's is
 * online, it calls `nfc.read` or `code.scan` on it (`DeviceCall`), then gives the call's
 * `read_id` to the tag decoder (`POST /filament/tags/reads`) or the code decoder
 * (`POST /scan/codes`), and links what the read names. With no device online it offers the
 * pairing QR code (`AddDevice`). Where core does not serve the devices routes (404), it shows nothing.
 */
export function UseMyPhone() {
  const [pairingOpen, setPairingOpen] = useState(false)
  const devices = usePolledDevices(pairingOpen ? PAIRING_POLL_MS : LIST_POLL_MS)
  const [chosen, setChosen] = useState<string | null>(null)
  const [run, setRun] = useState<Run | null>(null)
  const [decoded, setDecoded] = useState<Decoded | null>(null)
  const [decoding, setDecoding] = useState(false)
  const [calling, setCalling] = useState(false)
  const runs = useRef(0)
  const pickerId = useId()

  if (devices.error instanceof ApiError && devices.error.status === 404) return null
  if (!devices.data) return null

  const usable = devices.data.filter((one) => one.online && OFFERED.some(({ method }) => one.capabilities.includes(method)))
  const device = usable.find((one) => one.id === chosen) ?? usable[0]
  /** Paired phones that could read here once their app is open. */
  const offline = devices.data.filter((one) => !one.online && OFFERED.some(({ method }) => one.capabilities.includes(method)))
  const busy = calling || decoding

  function begin(method: DeviceMethod) {
    if (!device) return
    runs.current += 1
    setDecoded(null)
    setCalling(true)
    setRun({ key: runs.current, deviceId: device.id, method })
  }

  async function ended(method: DeviceMethod, end: CallEnd) {
    setCalling(false)
    const readId = end.result?.read_id
    if (end.outcome !== 'done' || !readId) return
    setDecoding(true)
    try {
      if (method === 'nfc.read') {
        setDecoded({ kind: 'tag', result: await tagReadFromDevice(readId) })
      } else {
        const scan = await codeScanFromDevice(readId)
        setDecoded({ kind: 'codes', codes: scan.codes, error: scan.error })
      }
    } catch (cause) {
      setDecoded({ kind: 'failed', message: `The phone read it, but ScadBuddy could not decode the read: ${cause instanceof Error ? cause.message : String(cause)}` })
    } finally {
      setDecoding(false)
    }
  }

  return (
    <section aria-labelledby={`${pickerId}-heading`} className="mt-3 flex flex-col gap-3 rounded-lg border border-line p-3">
      <div className="flex flex-col gap-0.5">
        <h2 id={`${pickerId}-heading`} className="font-medium">
          Use my phone
        </h2>
        <p className="text-muted-foreground">
          {device
            ? 'Read a tag or a code with the ScadBuddy app on a paired phone.'
            : offline.length > 0
              ? `${offline.map((one) => one.name).join(', ')} ${offline.length === 1 ? 'is' : 'are'} offline. Open the ScadBuddy app on the phone to use it here, or pair another phone.`
              : 'Pair a phone with the ScadBuddy app, then read tags and codes with it from here.'}
        </p>
      </div>
      {!device ? (
        <AddDevice devices={devices.data} onPairing={setPairingOpen} />
      ) : (
        <>
          {usable.length > 1 && (
            <div className="flex items-center gap-2">
              <span id={`${pickerId}-label`}>Phone</span>
              <Select value={device.id} onValueChange={setChosen}>
                <SelectTrigger aria-labelledby={`${pickerId}-label`} className="min-w-0">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {usable.map((one) => (
                    <SelectItem key={one.id} value={one.id}>
                      {one.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            {OFFERED.filter(({ method }) => device.capabilities.includes(method)).map(({ method, label, icon: Icon }) => (
              <Button key={method} type="button" variant="outline" onClick={() => begin(method)} disabled={busy}>
                <Icon aria-hidden="true" /> {label}
              </Button>
            ))}
          </div>
          {usable.length === 1 && <p className="text-muted-foreground">On {device.name}.</p>}
        </>
      )}
      {run && <DeviceCall key={run.key} deviceId={run.deviceId} method={run.method} onEnd={(end) => void ended(run.method, end)} />}
      {decoding && <p role="status">Decoding the read…</p>}
      {decoded && <DecodedRead decoded={decoded} />}
    </section>
  )
}

function DecodedRead({ decoded }: { decoded: Decoded }) {
  if (decoded.kind === 'failed') {
    return (
      <p role="alert" className="text-warn">
        {decoded.message}
      </p>
    )
  }
  if (decoded.kind === 'codes') {
    if (decoded.codes.length === 0) {
      const reason = decoded.error ? (SCAN_ERROR[decoded.error] ?? `ScadBuddy could not decode the photo (${decoded.error}). Try again.`) : null
      return <p role="status">{reason ?? 'The photo has no code that ScadBuddy can read. Try again.'}</p>
    }
    return (
      <ul aria-label="Codes read" className="flex flex-col gap-1">
        {decoded.codes.map((code, at) => (
          <li key={`${at}-${code.text}`}>
            {code.resolved ? (
              <Link to={codeTarget(code.resolved)} className="font-medium text-primary underline underline-offset-4">
                Open {codeLabel(code.resolved)}
              </Link>
            ) : (
              <span>
                {code.text} <span className="text-muted-foreground">(names nothing in ScadBuddy)</span>
              </span>
            )}
          </li>
        ))}
      </ul>
    )
  }
  const { result } = decoded
  if (result.spool) {
    return (
      <p role="status">
        <Link to={filamentLinks.spool(result.spool.id)} className="font-medium text-primary underline underline-offset-4">
          Open spool #{result.spool.number}
        </Link>
      </p>
    )
  }
  if (result.resolved) {
    return (
      <p role="status">
        <Link to={codeTarget(result.resolved)} className="font-medium text-primary underline underline-offset-4">
          Open {codeLabel(result.resolved)}
        </Link>
      </p>
    )
  }
  return <p role="status">{result.next === 'none' ? 'The tag has no serial number.' : 'A new tag: no spool holds it yet.'}</p>
}
