import { CameraOffIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { QR_FORMATS, readableFormats, type BarcodeDetectorClass, type BarcodeDetectorLike } from './camera'
import { Button } from '@/components/ui/button'

/**
 * The rear camera, read in the browser by `BarcodeDetector` until it finds a code; the camera
 * stops then. `formats` are the code formats to read (a QR code by default), kept to the ones the
 * detector says it reads.
 */
export function CameraReader({ Detector, formats = QR_FORMATS, onRead, onStop, onFail }: { Detector: BarcodeDetectorClass; formats?: readonly string[]; onRead: (raw: string) => void; onStop: () => void; onFail: (reason: string) => void }) {
  const video = useRef<HTMLVideoElement>(null)
  // The formats the reader started with; a new array each render does not restart the camera.
  const [wanted] = useState(formats)
  const done = useRef({ onRead, onFail })
  useEffect(() => {
    done.current = { onRead, onFail }
  }, [onRead, onFail])

  useEffect(() => {
    let alive = true
    let stream: MediaStream | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    const stop = () => stream?.getTracks().forEach((track) => track.stop())
    let detector: BarcodeDetectorLike | null = null
    async function tick() {
      if (!alive || !video.current) return
      if (!detector) {
        try {
          detector = new Detector({ formats: await readableFormats(Detector, wanted) })
        } catch {
          if (!alive) return
          alive = false
          stop()
          done.current.onFail('This browser cannot read codes from the camera. Type the code from the label.')
          return
        }
        if (!alive || !video.current) return
      }
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
        // A frame not ready yet: try the next frame.
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
  }, [Detector, wanted])

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
