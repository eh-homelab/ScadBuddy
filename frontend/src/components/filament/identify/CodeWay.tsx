import { CameraIcon, ImageUpIcon } from 'lucide-react'
import { useId, useRef, useState, type ReactNode } from 'react'
import { ApiError } from '../../../api/client'
import { decodeScan, storeScanImage } from '../../../api/scan'
import { scanSearchQuery } from '../../../lib/scanSearch'
import { detectorClass, jpegOf } from './camera'
import { CameraReader } from './CameraReader'
import { reasonOf } from './reason'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

/** A sentence that says why nothing is named, or what to do. */
export function Said({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="text-[13px] text-warn">
      {children}
    </p>
  )
}

/** The words of a code way: the field's label and example, and the sentence for a photo with no label of the kind. */
export interface CodeCopy {
  label: string
  placeholder: string
  noLabel: string
}

/** #8372: a text that names no thing, to search for in its place. */
export interface CodeSearch {
  search: string
}

const isSearch = (found: object): found is CodeSearch => 'search' in found && typeof found.search === 'string'

/**
 * The code way of `IdentifySpool` and `IdentifyPlace`: a typed code, always; the camera where
 * `BarcodeDetector` exists and `camera` allows it (not in a frame), else a photo the server
 * reads (#8155), which a 404 hides. `fromText` resolves a typed or scanned text, `fromCode` the
 * code the photo's first code of `kind` names; each gives the thing, or the sentence why none.
 * `onIdentified` gets null as soon as nothing is named any more. When a lookup gives a
 * search (#8372), the way shows nothing and gives the search to `onSearch`. With `onSearch`, a
 * photo with no code of `kind` but a code that names no thing searches that code.
 */
export function CodeWay<T extends object>({
  kind,
  copy,
  fromText,
  fromCode,
  onIdentified,
  onSearch,
  camera: cameraAllowed,
}: {
  kind: 'spool' | 'location'
  copy: CodeCopy
  fromText: (text: string) => Promise<T | string | CodeSearch>
  fromCode: (code: string) => Promise<T | string | CodeSearch>
  onIdentified: (found: T | null) => void
  onSearch?: (text: string) => void
  camera: boolean
}) {
  const [text, setText] = useState('')
  const [said, setSaid] = useState<string | null>(null)
  const [looking, setLooking] = useState(false)
  const [photoBusy, setPhotoBusy] = useState(false)
  const [camera, setCamera] = useState(false)
  const [photosOff, setPhotosOff] = useState(false)
  const [Detector] = useState(() => (cameraAllowed ? detectorClass() : null))
  const photo = useRef<HTMLInputElement>(null)
  const ids = useId()

  /** The answer of a lookup: the thing, or the sentence why none, which also names none. */
  function answer(found: T | string | CodeSearch) {
    if (typeof found === 'string') {
      setSaid(found)
      onIdentified(null)
    } else if (isSearch(found)) {
      onIdentified(null)
      onSearch?.(found.search)
    } else onIdentified(found)
  }

  async function look(raw: string) {
    setLooking(true)
    setSaid(null)
    onIdentified(null)
    const found = await fromText(raw)
    setLooking(false)
    answer(found)
  }

  async function readPhoto(file: File) {
    setPhotoBusy(true)
    setSaid(null)
    onIdentified(null)
    /** #8372: the search for a code the photo read that names no thing. */
    let search: string | null = null
    try {
      const stored = await storeScanImage(await jpegOf(file))
      const scan = await decodeScan({ key: stored.key, source: 'shutter', location: null })
      const first = scan.codes.find((code) => code.resolved?.kind === kind)
      const unknown = onSearch ? scan.codes.find((code) => !code.resolved) : undefined
      if (first?.resolved) answer(await fromCode(first.resolved.code))
      else if (unknown) search = scanSearchQuery(unknown.text)
      else answer(scan.error ? 'The photo could not be read. Take it again, closer to the label.' : copy.noLabel)
    } catch (cause) {
      // The core area is off: no photo route on this server.
      if (cause instanceof ApiError && cause.status === 404) {
        setPhotosOff(true)
        answer('This server reads no photos. Type the code from the label.')
      } else answer(cause instanceof ApiError ? `The photo did not go through: ${cause.detail}.` : reasonOf(cause, 'The photo did not go through. Try again.'))
    } finally {
      setPhotoBusy(false)
    }
    // After the busy flag: the search can remove this way from the page.
    if (search !== null) answer({ search })
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed px-3 py-2.5">
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          if (text.trim()) void look(text)
        }}
      >
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${ids}-code`}>{copy.label}</Label>
          <Input
            id={`${ids}-code`}
            value={text}
            placeholder={copy.placeholder}
            autoComplete="off"
            onChange={(event) => {
              setText(event.target.value)
              setSaid(null)
              // Another code: the thing the old one named is no longer named.
              onIdentified(null)
            }}
            className="w-40"
          />
        </div>
        <Button type="submit" variant="outline" disabled={looking || !text.trim()}>
          Find
        </Button>
        {Detector && !camera && (
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              setCamera(true)
              onIdentified(null)
            }}
          >
            <CameraIcon /> Use camera
          </Button>
        )}
        {!Detector && !photosOff && (
          <>
            <Button type="button" variant="outline" disabled={photoBusy} onClick={() => photo.current?.click()}>
              <ImageUpIcon /> {photoBusy ? 'Reading the photo…' : 'Take a photo'}
            </Button>
            <input
              ref={photo}
              type="file"
              accept="image/*"
              capture="environment"
              aria-label="Photo of the label"
              className="sr-only"
              tabIndex={-1}
              onChange={(event) => {
                const file = event.target.files?.[0]
                event.target.value = ''
                if (file) void readPhoto(file)
              }}
            />
          </>
        )}
      </form>
      {camera && Detector && (
        <CameraReader
          Detector={Detector}
          onRead={(raw) => {
            setCamera(false)
            void look(raw)
          }}
          onStop={() => setCamera(false)}
          onFail={(reason) => {
            setCamera(false)
            setSaid(reason)
          }}
        />
      )}
      {said && <Said>{said}</Said>}
    </div>
  )
}
