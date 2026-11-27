import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { type CodeScan, type ScanSource, decodeScan, storeScanImage } from '../../../api/scan'
import {
  type CameraState,
  type CapturedScan,
  type ScanCamera,
  FILE_STILL_MAX,
  PREVIEW_STILL_MAX,
  ScanCameraContext,
  cameraRefusal,
  stillOf,
  whereNow,
} from './scanCamera'

/** The page's camera and its scans (#8155), for `ScanCode` and for any read that wants a photo (`useScanCamera`). */
export function ScanCameraProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<CameraState>('off')
  const [reason, setReason] = useState<string | null>(null)
  const [scans, setScans] = useState<CodeScan[]>([])
  const stream = useRef<MediaStream | null>(null)
  const video = useRef<HTMLVideoElement | null>(null)
  // The camera stops while the page is hidden, and starts again when it is shown.
  const resume = useRef(false)
  // Each start, stop and unmount bumps it: a camera that opens after it moved is not wanted.
  const generation = useRef(0)
  const opening = useRef(false)

  const attach = useCallback(() => {
    const element = video.current
    if (!element || !stream.current || element.srcObject === stream.current) return
    element.srcObject = stream.current
    void element.play().catch(() => undefined)
  }, [])

  const stop = useCallback(() => {
    generation.current += 1
    opening.current = false
    stream.current?.getTracks().forEach((track) => track.stop())
    stream.current = null
    if (video.current) video.current.srcObject = null
    setState((now) => (now === 'unavailable' ? now : 'off'))
  }, [])

  const start = useCallback(async () => {
    if (stream.current || opening.current) return
    if (!navigator.mediaDevices?.getUserMedia) {
      setState('unavailable')
      setReason('This browser gives the page no camera. Choose a photo instead.')
      return
    }
    const mine = ++generation.current
    opening.current = true
    setState('starting')
    setReason(null)
    try {
      const opened = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } },
        audio: false,
      })
      if (mine !== generation.current) {
        opened.getTracks().forEach((track) => track.stop())
        return
      }
      stream.current = opened
      setState('on')
      attach()
    } catch (cause) {
      if (mine !== generation.current) return
      setState('unavailable')
      setReason(cameraRefusal(cause))
    } finally {
      if (mine === generation.current) opening.current = false
    }
  }, [attach])

  useEffect(() => {
    const changed = () => {
      if (document.hidden && (stream.current || opening.current)) {
        resume.current = true
        stop()
      } else if (!document.hidden && resume.current) {
        resume.current = false
        void start()
      }
    }
    document.addEventListener('visibilitychange', changed)
    return () => {
      document.removeEventListener('visibilitychange', changed)
      generation.current += 1
      stream.current?.getTracks().forEach((track) => track.stop())
      stream.current = null
    }
  }, [start, stop])

  const bindVideo = useCallback(
    (element: HTMLVideoElement | null) => {
      video.current = element
      attach()
    },
    [attach],
  )

  const scanStill = useCallback(async (still: Blob, source: ScanSource): Promise<CapturedScan> => {
    // The fix is asked for at once and waited for after the upload, at most LOCATION_WAIT_MS.
    const place = whereNow()
    const image = await storeScanImage(still)
    const scan = await decodeScan({ key: image.key, source, location: await place })
    setScans((now) => [scan, ...now])
    return { key: image.key, mediaType: image.media_type, scan }
  }, [])

  const capture = useCallback(
    async (source: ScanSource = 'shutter') => {
      const element = video.current
      if (!stream.current || !element || element.videoWidth === 0) return null
      const still = await stillOf(element, element.videoWidth, element.videoHeight, PREVIEW_STILL_MAX)
      return scanStill(still, source)
    },
    [scanStill],
  )

  const scanFile = useCallback(
    async (file: File) => {
      let bitmap: ImageBitmap
      try {
        bitmap = await createImageBitmap(file)
      } catch {
        throw new Error('This browser cannot open that photo. Choose a JPEG or PNG.')
      }
      try {
        return await scanStill(await stillOf(bitmap, bitmap.width, bitmap.height, FILE_STILL_MAX), 'file')
      } finally {
        bitmap.close()
      }
    },
    [scanStill],
  )

  const replace = useCallback((scan: CodeScan) => setScans((now) => now.map((one) => (one.id === scan.id ? scan : one))), [])
  const forget = useCallback((id: string) => setScans((now) => now.filter((one) => one.id !== id)), [])

  const value = useMemo<ScanCamera>(
    () => ({ state, reason, start, stop, bindVideo, capture, scanFile, scans, replace, forget }),
    [state, reason, start, stop, bindVideo, capture, scanFile, scans, replace, forget],
  )
  return <ScanCameraContext.Provider value={value}>{children}</ScanCameraContext.Provider>
}
