import { CameraOffIcon } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { FILE_STILL_MAX, stillOf } from '../scan/scanCamera'
import { Button } from '@/components/ui/button'

/** The Shape Detection API's detector, where the browser has one (Chrome on Android). */
export interface BarcodeDetectorLike {
  detect(source: HTMLVideoElement): Promise<{ rawValue: string }[]>
}
export type BarcodeDetectorClass = new (options?: { formats: string[] }) => BarcodeDetectorLike

export const detectorClass = (): BarcodeDetectorClass | null => (window as unknown as { BarcodeDetector?: BarcodeDetectorClass }).BarcodeDetector ?? null

/** The rear camera, read in the browser by `BarcodeDetector` until it finds a code; the camera stops then. */
export function CameraReader({ Detector, onRead, onStop, onFail }: { Detector: BarcodeDetectorClass; onRead: (raw: string) => void; onStop: () => void; onFail: (reason: string) => void }) {
  const video = useRef<HTMLVideoElement>(null)
  const done = useRef({ onRead, onFail })
  useEffect(() => {
    done.current = { onRead, onFail }
  }, [onRead, onFail])

  useEffect(() => {
    let alive = true
    let stream: MediaStream | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    const stop = () => stream?.getTracks().forEach((track) => track.stop())
    const detector = new Detector({ formats: ['qr_code'] })
    async function tick() {
      if (!alive || !video.current) return
      try {
        const found = await detector.detect(video.current)
        const raw = found.find((one) => one.rawValue)?.rawValue
        if (raw && alive) {
          alive = false
          stop()
          done.current.onRead(raw)
          return
        }
      } catch {
        // A frame not ready yet: try the next one.
      }
      if (alive) timer = setTimeout(() => void tick(), 300)
    }
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: 'environment' } })
      .then((got) => {
        stream = got
        if (!alive) return stop()
        const element = video.current
        if (element) {
          element.srcObject = got
          try {
            void Promise.resolve(element.play()).catch(() => {})
          } catch {
            // The preview may stay still; the detector reads the frames anyway.
          }
        }
        void tick()
      })
      .catch((cause: unknown) => {
        if (alive) done.current.onFail(cause instanceof DOMException && cause.name === 'NotAllowedError' ? 'The camera is not allowed for this page. Type the code from the label, or allow the camera in the browser’s site settings.' : 'The camera did not start. Type the code from the label.')
      })
    return () => {
      alive = false
      clearTimeout(timer)
      stop()
    }
  }, [Detector])

  return (
    <div className="flex flex-col gap-2">
      <div className="relative aspect-[4/3] max-h-56 overflow-hidden rounded-lg bg-black">
        <video ref={video} muted playsInline aria-label="Camera preview" className="size-full object-cover" />
        <div aria-hidden="true" className="pointer-events-none absolute inset-[18%] rounded-md border-2 border-white/80" />
      </div>
      <Button type="button" variant="ghost" size="sm" className="self-start" onClick={onStop}>
        <CameraOffIcon /> Stop camera
      </Button>
    </div>
  )
}

/** A label's photo drawn again as a JPEG of a known size (core takes JPEG, PNG or WebP; a phone may give HEIC). */
export async function jpegOf(file: File): Promise<Blob> {
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    throw new Error('This browser cannot open that photo. Type the code from the label.')
  }
  try {
    return await stillOf(bitmap, bitmap.width, bitmap.height, FILE_STILL_MAX)
  } finally {
    bitmap.close()
  }
}
