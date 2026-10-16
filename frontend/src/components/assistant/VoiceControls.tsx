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
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { Toggle } from '@/components/ui/toggle'
import { Tip } from './Tip'
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
      <Tip wrap={disabled} content="Bambuddy doesn’t give its embedded pages the microphone">
        <Button
          size="sm"
          variant="ghost"
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
      </Tip>
    )
  }

  const onPointerDown = (event: PointerEvent) => {
    if (event.button !== 0 || disabled) return
    pressed.current = { at: performance.now(), started: !dictation.listening }
    if (!dictation.listening) dictation.start()
  }
  const onPointerUp = () => {
    const press = pressed.current
    pressed.current = null
    if (!press) return
    if (!press.started || performance.now() - press.at >= HOLD_MS) dictation.stop()
  }
  // Pointer presses are handled above; a keyboard press (detail 0) toggles.
  const onClick = (event: MouseEvent) => {
    if (event.detail !== 0) return
    if (dictation.listening) dictation.stop()
    else dictation.start()
  }

  return (
    <Tip wrap={disabled} content="Press to talk and press again to stop, or hold while you speak">
      {/* Pressed while listening; the presses are handled above, not by the toggle itself. */}
      <Toggle
        size="sm"
        variant="outline"
        className="data-[state=on]:border-primary data-[state=on]:bg-primary data-[state=on]:text-primary-foreground"
        aria-label="Voice input"
        pressed={dictation.listening}
        aria-describedby={describedBy}
        disabled={disabled}
        onPointerDown={onPointerDown}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onClick={onClick}
        {...USER_ONLY}
      >
        {dictation.listening && (
          <span aria-hidden="true" className="h-2 w-2 rounded-full bg-primary-foreground motion-safe:animate-pulse" />
        )}
        <MicIcon />
      </Toggle>
    </Tip>
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
    <p id={id} className="mt-1 text-[11px] text-muted-foreground">
      Voice input is transcribed by your browser, which may send the audio to its maker’s servers.
    </p>
  )
}

/** "Read replies aloud": per browser, off by default, user-only. Hidden without speechSynthesis. */
export function SpeakRepliesToggle() {
  const on = useSpeakReplies()
  if (!speechSynthesisSupported()) return null
  return (
    <label className="flex shrink-0 items-center gap-1.5 text-[11.5px] whitespace-nowrap text-muted-foreground" {...USER_ONLY}>
      <Switch size="sm" checked={on} onCheckedChange={setSpeakReplies} {...USER_ONLY} />
      Read replies aloud
    </label>
  )
}
