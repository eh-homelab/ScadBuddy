import { useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { ApiError } from '../api/client'
import { revokeDevice, type Device } from '../api/devices'
import { AddDevice } from '../components/devices/AddDevice'
import { LIST_POLL_MS, METHOD_LABEL, PAIRING_POLL_MS, usePolledDevices } from '../components/devices/useDevices'
import { PageLoading } from '../components/PageState'
import { Spinner } from '../components/ui/spinner'
import { timeAgo } from '../lib/format'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

const dayTime = (iso: string) => new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

/**
 * #7905 — the Devices page (spec §The Devices view, plan Task 9): the person's paired devices,
 * each online or offline with when it was last seen, what it can do, when its token expires
 * without use and when the pairing ends; "Add a device" (`AddDevice`) and Revoke.
 */
export function DevicesPage() {
  const [pairingOpen, setPairingOpen] = useState(false)
  const devices = usePolledDevices(pairingOpen ? PAIRING_POLL_MS : LIST_POLL_MS)
  const [confirming, setConfirming] = useState<Device | null>(null)
  const [revoking, setRevoking] = useState(false)
  const [revokeError, setRevokeError] = useState<string | null>(null)

  async function revoke() {
    if (!confirming) return
    setRevoking(true)
    setRevokeError(null)
    try {
      await revokeDevice(confirming.id)
      setConfirming(null)
      devices.reload()
    } catch (cause) {
      setRevokeError(cause instanceof ApiError && cause.status === 404 ? 'This device is no longer paired.' : `Could not revoke the device: ${cause instanceof Error ? cause.message : String(cause)}`)
    } finally {
      setRevoking(false)
    }
  }

  const notServed = devices.error instanceof ApiError && devices.error.status === 404

  return (
    <div className="mx-auto flex w-full max-w-3xl min-w-0 flex-col gap-6 px-4 py-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold tracking-tight">Devices</h1>
        <p className="text-muted-foreground">
          A paired phone reads NFC tags, scans codes and takes photos for ScadBuddy. Open the ScadBuddy app on the phone to keep it online.
        </p>
      </div>

      {notServed ? (
        <p role="status" className="text-muted-foreground">
          This ScadBuddy does not have devices yet.
        </p>
      ) : (
        <>
          <AddDevice devices={devices.data} onPairing={setPairingOpen} />
          {devices.loading ? (
            <PageLoading label="Loading devices" rows={2} />
          ) : devices.error && !devices.data ? (
            <div role="alert" className="flex flex-col items-start gap-2">
              <p className="text-warn">Could not read the devices: {devices.error.message}</p>
              <Button type="button" variant="outline" size="sm" onClick={devices.reload}>
                Try again
              </Button>
            </div>
          ) : devices.data && devices.data.length === 0 ? (
            <p className="text-muted-foreground">No devices are paired. Add a device to use a phone with ScadBuddy.</p>
          ) : (
            <ul aria-label="Paired devices" className="flex flex-col divide-y divide-line rounded-lg border border-line">
              {devices.data?.map((device) => (
                <DeviceRow key={device.id} device={device} onRevoke={() => setConfirming(device)} />
              ))}
            </ul>
          )}
        </>
      )}

      <AlertDialog open={confirming !== null} onOpenChange={(open) => !open && !revoking && (setConfirming(null), setRevokeError(null))}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{`Revoke ${confirming?.name ?? 'device'}?`}</AlertDialogTitle>
            <AlertDialogDescription>The device stops at once. To use it again, pair it again.</AlertDialogDescription>
          </AlertDialogHeader>
          {revokeError && (
            <p role="alert" className="text-warn">
              {revokeError}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={revoking}>Cancel</AlertDialogCancel>
            {/* Not an AlertDialogAction: the dialog stays open until the revoke answers. */}
            <Button variant="destructive" onClick={() => void revoke()} disabled={revoking} aria-busy={revoking} {...USER_ONLY}>
              {revoking ? <Spinner /> : 'Revoke device'}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

function DeviceRow({ device, onRevoke }: { device: Device; onRevoke: () => void }) {
  return (
    <li className="flex flex-col gap-3 p-4 sm:flex-row sm:items-start sm:justify-between">
      <div className="flex min-w-0 flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2 className="font-medium">{device.name}</h2>
          <OnlineState device={device} />
        </div>
        <p className="text-muted-foreground">
          {device.model}, app {device.app_version}
        </p>
        <ul aria-label={`What ${device.name} can do`} className="flex flex-wrap gap-1.5">
          {device.capabilities.map((method) => (
            <li key={method} className="rounded-md border border-line px-2 py-0.5 text-xs">
              {METHOD_LABEL[method] ?? method}
            </li>
          ))}
        </ul>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
          <dt>Expires if not used</dt>
          <dd>{device.expires_at ? dayTime(device.expires_at) : 'Not stated'}</dd>
          <dt>Pairing ends</dt>
          <dd>{device.hard_cap_at ? dayTime(device.hard_cap_at) : 'Not stated'}</dd>
        </dl>
      </div>
      <div>
        <Button type="button" variant="outline" size="sm" onClick={onRevoke} aria-label={`Revoke ${device.name}`} {...USER_ONLY}>
          Revoke
        </Button>
      </div>
    </li>
  )
}

/** Online, or offline with when the device was last seen. */
export function OnlineState({ device }: { device: Device }) {
  return (
    <span className="flex items-center gap-1.5 text-xs">
      <span aria-hidden="true" className={cn('size-2 rounded-full', device.online ? 'bg-ok' : 'bg-faint')} />
      <span className={cn(device.online ? 'text-ok' : 'text-muted-foreground')}>
        {device.online ? 'Online' : 'Offline'}
        {!device.online && device.last_seen_at && `, last seen ${timeAgo(device.last_seen_at)}`}
        {!device.online && !device.last_seen_at && ', never seen'}
      </span>
    </span>
  )
}
