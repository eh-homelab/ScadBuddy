import { useRef, type MouseEvent, type PointerEvent } from 'react'
import { USER_ONLY } from '../../agent/dom'
import {
  microphoneAllowedByPolicy,
  recognitionCtor,
  setSpeakReplies,
  speechSynthesisSupported,
  useSpeakReplies,
} from '../../agent/chat/voice'
import { openExternal } from '../../lib/embed'
import { Button } from '../ui/Button'
import type { Dictation } from './useVoice'

/** A press held at least this long is push-to-talk: letting go stops listening. */
const HOLD_MS = 400

function MicIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="5.5" y="1.5" width="5" height="8" rx="2.5" />
      <path d="M3 7.5a5 5 0 0 0 10 0M8 12.5v2" strokeLinecap="round" />
    </svg>
  )
}

/**
 * The composer's microphone. Hidden where the browser has no speech recognition. Inside
 * Bambuddy's iframe, where the Permissions Policy denies the microphone (or a start was
 * refused), it becomes a link out to a new tab instead.
 *
 * Press to start and press again to stop, or hold it down and let go (push-to-talk). The
 * keyboard toggles. It is user-only: an agent can never turn the microphone on.
 */
export function MicButton({
  dictation,
  disabled,
  embedded,
  describedBy,
}: {
  dictation: Dictation
  disabled: boolean
  embedded: boolean
  /** The id of the `VoiceDisclosure` note, read out with the mic. */
  describedBy?: string
}) {
  const pressed = useRef<{ at: number; started: boolean } | null>(null)
  if (!recognitionCtor()) return null
  if (embedded && (dictation.blocked || microphoneAllowedByPolicy() === false)) {
    return (
      <Button
        size="sm"
        variant="ghost"
        title="Bambuddy doesn’t give its embedded pages the microphone"
        disabled={disabled}
        aria-describedby={describedBy}
        onClick={() => {
          if (!disabled) openExternal(window.location.href, true)
        }}
        {...USER_ONLY}
      >
        <MicIcon />
        Open in a new tab to use voice
      </Button>
    )
  }

  const onPointerDown = (event: PointerEvent) => {
    if (event.button !== 0 || disabled) return
    pressed.current = { at: Date.now(), started: !dictation.listening }
    if (!dictation.listening) dictation.start()
  }
  const onPointerUp = () => {
    const press = pressed.current
    pressed.current = null
    if (!press) return
    if (!press.started || Date.now() - press.at >= HOLD_MS) dictation.stop()
  }
  // Pointer presses are handled above; a keyboard press (detail 0) toggles.
  const onClick = (event: MouseEvent) => {
    if (event.detail !== 0) return
    if (dictation.listening) dictation.stop()
    else dictation.start()
  }

  return (
    <Button
      size="sm"
      variant={dictation.listening ? 'primary' : 'default'}
      aria-label="Voice input"
      aria-pressed={dictation.listening}
      aria-describedby={describedBy}
      title="Press to talk and press again to stop, or hold while you speak"
      disabled={disabled}
      onPointerDown={onPointerDown}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onClick={onClick}
      {...USER_ONLY}
    >
      {dictation.listening && (
        <span aria-hidden="true" className="h-2 w-2 rounded-full bg-accent-ink motion-safe:animate-pulse" />
      )}
      <MicIcon />
    </Button>
  )
}

/**
 * Says where the audio goes. The browser, not ScadBuddy, does the speech to text, and
 * Chrome's engine is server-based: "On some browsers, like Chrome, using Speech
 * Recognition on a web page involves a server-based recognition engine. Your audio is
 * sent to a web service for recognition processing" (MDN,
 * https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition). Shown wherever the
 * mic (or its new-tab fallback) is, and tied to it with `aria-describedby`.
 */
export function VoiceDisclosure({ id }: { id: string }) {
  if (!recognitionCtor()) return null
  return (
    <p id={id} className="mt-1 text-[11px] text-faint">
      Voice input is transcribed by your browser, which may send the audio to its maker’s servers.
    </p>
  )
}

/** "Read replies aloud": per browser, off by default, user-only. Hidden without speechSynthesis. */
export function SpeakRepliesToggle() {
  const on = useSpeakReplies()
  if (!speechSynthesisSupported()) return null
  return (
    <label className="flex items-center gap-1.5 text-[11.5px] text-muted" {...USER_ONLY}>
      <input type="checkbox" checked={on} onChange={(event) => setSpeakReplies(event.target.checked)} {...USER_ONLY} />
      Read replies aloud
    </label>
  )
}
