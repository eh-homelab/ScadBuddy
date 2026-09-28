import { useCallback, useEffect, useRef, useState } from 'react'
import type { FeedItem } from '../../agent/chat/state'
import {
  recognitionCtor,
  recognitionErrorMessage,
  speechSynthesisSupported,
  spokenText,
  type Recognition,
} from '../../agent/chat/voice'

function joinText(base: string, spoken: string): string {
  if (!base) return spoken
  if (!spoken) return base
  return /\s$/.test(base) ? base + spoken : `${base} ${spoken}`
}

interface DictationOptions {
  getDraft: () => string
  setDraft: (text: string) => void
  /** Called when listening ends, so the user can review the text. Never sends it. */
  onDone: () => void
  /** Called just before listening starts (the panel stops speaking, so the mic doesn't hear it). */
  onStart?: () => void
  embedded: boolean
}

/**
 * Speech to text into the composer. The transcript is written into the draft as it
 * arrives (interim results included) and is never sent: the user reads it and presses
 * Send (#257).
 */
export function useDictation({ getDraft, setDraft, onDone, onStart, embedded }: DictationOptions) {
  const recognition = useRef<Recognition | null>(null)
  const [listening, setListening] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [blocked, setBlocked] = useState(false)
  const [announcement, setAnnouncement] = useState('')
  const latest = useRef({ setDraft, onDone, onStart })
  useEffect(() => {
    latest.current = { setDraft, onDone, onStart }
  })

  const start = useCallback(() => {
    const Ctor = recognitionCtor()
    if (!Ctor || recognition.current) return
    latest.current.onStart?.()
    const rec = new Ctor()
    rec.lang = document.documentElement.lang || navigator.language
    rec.continuous = true
    rec.interimResults = true
    const base = getDraft()
    rec.onresult = (event) => {
      let text = ''
      for (let i = 0; i < event.results.length; i++) text += event.results[i]?.[0].transcript ?? ''
      latest.current.setDraft(joinText(base, text.trim()))
    }
    rec.onerror = (event) => {
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        if (embedded) setBlocked(true)
      }
      const message = recognitionErrorMessage(event.error, embedded)
      if (message) setError(message)
    }
    rec.onend = () => {
      if (recognition.current !== rec) return
      recognition.current = null
      setListening(false)
      setAnnouncement('Stopped listening. Review the message, then send it.')
      latest.current.onDone()
    }
    recognition.current = rec
    setError(null)
    try {
      rec.start()
    } catch {
      recognition.current = null
      setError('Speech recognition could not start.')
      return
    }
    setListening(true)
    setAnnouncement('Listening. Speak, then stop the microphone.')
  }, [getDraft, embedded])

  const stop = useCallback(() => {
    recognition.current?.stop()
  }, [])

  /** Drops the recognition without touching the draft again, e.g. when the message is sent. */
  const cancel = useCallback(() => {
    const rec = recognition.current
    if (!rec) return
    recognition.current = null
    rec.abort()
    setListening(false)
  }, [])

  useEffect(() => () => recognition.current?.abort(), [])

  return { listening, error, blocked, announcement, start, stop, cancel }
}

export type Dictation = ReturnType<typeof useDictation>

/**
 * Spoken replies: once the toggle is on, the assistant's replies to a message the user
 * sent from this panel are read aloud. `arm()` is called on send; replies that were
 * already there, a replayed transcript, or another session's stream are not spoken.
 */
export function useSpokenReplies(sessionId: string | null, items: FeedItem[] | undefined, enabled: boolean) {
  const seen = useRef(new Set<string>())
  const fresh = useRef(new Set<string>())
  const armed = useRef<{ session: string | null } | null>(null)
  const [pending, setPending] = useState(0)

  const stop = useCallback(() => {
    if (!speechSynthesisSupported()) return
    window.speechSynthesis.cancel()
    setPending(0)
  }, [])

  const arm = useCallback(() => {
    armed.current = { session: sessionId }
  }, [sessionId])

  useEffect(() => {
    const turn = armed.current
    if (turn && turn.session !== sessionId) {
      // A new chat gets its id when it starts; any other change is the user moving away.
      if (turn.session === null && sessionId !== null) turn.session = sessionId
      else armed.current = null
    }
    for (const item of items ?? []) {
      if (item.kind !== 'assistant') continue
      if (!seen.current.has(item.id)) {
        seen.current.add(item.id)
        if (armed.current) fresh.current.add(item.id)
      }
      if (!item.done || !fresh.current.has(item.id)) continue
      fresh.current.delete(item.id)
      if (!enabled || !speechSynthesisSupported()) continue
      const text = spokenText(item.text)
      if (!text) continue
      const utterance = new SpeechSynthesisUtterance(text)
      utterance.lang = document.documentElement.lang || navigator.language
      const finished = () => setPending((n) => Math.max(0, n - 1))
      utterance.onend = finished
      utterance.onerror = finished
      setPending((n) => n + 1)
      window.speechSynthesis.speak(utterance)
    }
  }, [sessionId, items, enabled])

  useEffect(() => {
    if (!enabled) stop()
  }, [enabled, stop])

  useEffect(
    () => () => {
      if (speechSynthesisSupported()) window.speechSynthesis.cancel()
    },
    [],
  )

  return { speaking: pending > 0, stop, arm }
}
