import { useEffect } from 'react'
import { listDevices, type DeviceMethod } from '../../api/devices'
import { useAsync } from '../../lib/useAsync'

/** How often the list is read again: a device's heartbeat is every 30 s. */
export const LIST_POLL_MS = 15_000
/** While a pairing code is on screen, the list is read this often, to see the phone that paired. */
export const PAIRING_POLL_MS = 3_000

export const METHOD_LABEL: Record<DeviceMethod, string> = {
  'nfc.read': 'Read NFC tags',
  'nfc.write': 'Write NFC tags',
  'code.scan': 'Scan codes',
  'camera.capture': 'Take photos',
}

/** The person's devices, read now and every `ms`; a failed read keeps what is on screen. */
export function usePolledDevices(ms: number) {
  const state = useAsync(() => listDevices().then((answer) => answer.devices), [])
  const { refresh } = state
  useEffect(() => {
    const timer = setInterval(() => refresh(), ms)
    return () => clearInterval(timer)
  }, [ms, refresh])
  return state
}
