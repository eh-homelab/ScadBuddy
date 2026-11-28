import { useEffect, useRef, useState } from 'react'
import { NfcSession, type Reading } from '../../../lib/webNfc'
import { reasonOf } from './reason'

/**
 * Web NFC for a tag way: one scan while the way is open. Each tap calls `onTap` at once, then
 * `read`. Only the newest tap's answer reaches `onAnswer` (or sets the error), so a slow read
 * that answers after a newer tap changes nothing (#7168 review 6). Nothing is set once the way
 * is closed.
 */
export function useTagReads<T>({ read, onTap, onAnswer }: { read: (reading: Reading) => Promise<T>; onTap: () => void; onAnswer: (answer: T) => void }) {
  const [error, setError] = useState<string | null>(null)
  const latest = useRef({ read, onTap, onAnswer })
  useEffect(() => {
    latest.current = { read, onTap, onAnswer }
  })

  useEffect(() => {
    const session = new NfcSession()
    let alive = true
    let reads = 0
    const off = session.onReading((reading) => {
      const mine = (reads += 1)
      const newest = () => alive && mine === reads
      latest.current.onTap()
      latest.current.read(reading).then(
        (answer) => {
          if (!newest()) return
          setError(null)
          latest.current.onAnswer(answer)
        },
        (cause: unknown) => {
          if (newest()) setError(`ScadBuddy could not look up the tag: ${reasonOf(cause, 'try the tag again')}.`)
        },
      )
    })
    const offError = session.onReadingError(() => {
      if (alive) setError('This tag could not be read. Hold it to the phone again.')
    })
    session.start().catch((cause: unknown) => {
      if (!alive) return
      setError(cause instanceof DOMException && cause.name === 'NotAllowedError' ? "Allow NFC for this site in Chrome's settings, then choose Scan tag again." : `NFC could not start: ${reasonOf(cause, 'try again')}.`)
    })
    return () => {
      alive = false
      off()
      offError()
      session.stop()
    }
  }, [])

  return { error, setError }
}
