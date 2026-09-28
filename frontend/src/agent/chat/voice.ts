import { useSyncExternalStore } from 'react'
import { parseBlocks } from './markdownBlocks'

/**
 * Voice for the assistant panel (#257): speech to text through the browser's Web Speech
 * API, spoken replies through `speechSynthesis`. Nothing is transcribed server-side.
 *
 * - SpeechRecognition: https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition
 *   (Chrome ships it as `webkitSpeechRecognition` and sends the audio to a server;
 *   Safari has it; Firefox keeps it off. https://caniuse.com/speech-recognition)
 * - SpeechSynthesis: https://developer.mozilla.org/en-US/docs/Web/API/SpeechSynthesis
 */

/** The slice of `SpeechRecognition` the panel uses; TypeScript's DOM lib has no types for it. */
export interface Recognition {
  lang: string
  continuous: boolean
  interimResults: boolean
  onresult: ((event: RecognitionResultEvent) => void) | null
  onerror: ((event: { error: string }) => void) | null
  onend: (() => void) | null
  start(): void
  stop(): void
  abort(): void
}

export interface RecognitionResultEvent {
  resultIndex: number
  results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>
}

export type RecognitionCtor = new () => Recognition

export function recognitionCtor(): RecognitionCtor | undefined {
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor }
  return w.SpeechRecognition ?? w.webkitSpeechRecognition
}

export function speechSynthesisSupported(): boolean {
  return typeof window.speechSynthesis !== 'undefined' && typeof window.SpeechSynthesisUtterance === 'function'
}

/**
 * Whether this document's Permissions Policy lets it use the microphone: `true`/`false`
 * where the browser can say (Chromium's `document.permissionsPolicy`, or the older
 * `document.featurePolicy`), `undefined` where it can't (Safari, Firefox).
 *
 * `microphone`'s default allowlist is `self`, so a cross-origin iframe gets it only when
 * the parent's `<iframe allow="microphone">` grants it. Bambuddy's iframe carries no
 * `allow` (#257). https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Permissions-Policy/microphone
 */
export function microphoneAllowedByPolicy(): boolean | undefined {
  const d = document as unknown as Record<string, { allowsFeature?: (f: string) => boolean } | undefined>
  const policy = d.permissionsPolicy ?? d.featurePolicy
  if (typeof policy?.allowsFeature !== 'function') return undefined
  try {
    return policy.allowsFeature('microphone')
  } catch {
    return undefined
  }
}

/** A clear sentence for each `SpeechRecognitionErrorEvent.error`; `undefined` for the quiet ones. */
export function recognitionErrorMessage(error: string, embedded: boolean): string | undefined {
  switch (error) {
    case 'aborted':
      return undefined
    case 'no-speech':
      return 'No speech was heard. Press the microphone and try again.'
    case 'audio-capture':
      return 'No microphone was found, or it is in use by another app.'
    case 'not-allowed':
    case 'service-not-allowed':
      return embedded
        ? 'The microphone is blocked inside Bambuddy. Open ScadBuddy in a new tab to use voice.'
        : 'Microphone access was denied. Allow it in the browser’s site settings to use voice.'
    case 'network':
      return 'Speech recognition needs a network connection in this browser, and it failed.'
    case 'language-not-supported':
      return 'This browser can’t recognise speech in your language.'
    default:
      return `Speech recognition stopped (${error}).`
  }
}

/** Replies are spoken up to about this many characters; the rest stays on screen. */
export const SPOKEN_LIMIT = 400

/** Plain text for the voice: no code blocks, no markdown marks, cut at a sentence end. */
export function spokenText(markdown: string): string {
  const parts: string[] = []
  for (const block of parseBlocks(markdown)) {
    if (block.kind === 'code') continue
    if (block.kind === 'list') parts.push(...block.items)
    else parts.push(block.text)
  }
  const plain = parts
    .map((p) =>
      p
        .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
        .replace(/`([^`]*)`/g, '$1')
        .replace(/[*_~#>]+/g, '')
        .trim(),
    )
    .filter(Boolean)
    .map((p) => (/[.!?:]$/.test(p) ? p : `${p}.`))
    .join(' ')
  if (plain.length <= SPOKEN_LIMIT) return plain
  const cut = plain.slice(0, SPOKEN_LIMIT)
  const end = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('! '), cut.lastIndexOf('? '))
  return `${end > 0 ? cut.slice(0, end + 1) : cut.trimEnd() + '…'} The rest is in the panel.`
}

/**
 * The per-browser "Read replies aloud" choice. Off unless the user turns it on; stored in
 * localStorage, and storage that throws (private mode, a sandboxed iframe) keeps it in
 * memory for this page only, as `webmcpPreference.ts` does.
 */
export const SPEAK_REPLIES_KEY = 'scadbuddy.voice.speakReplies'

const listeners = new Set<() => void>()
let fallback = false

export function isSpeakRepliesOn(): boolean {
  try {
    return window.localStorage.getItem(SPEAK_REPLIES_KEY) === 'on'
  } catch {
    return fallback
  }
}

export function setSpeakReplies(on: boolean) {
  fallback = on
  try {
    if (on) window.localStorage.setItem(SPEAK_REPLIES_KEY, 'on')
    else window.localStorage.removeItem(SPEAK_REPLIES_KEY)
  } catch {
    // Kept in memory for this page only.
  }
  for (const listener of listeners) listener()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  const onStorage = (event: StorageEvent) => {
    if (event.key === SPEAK_REPLIES_KEY || event.key === null) listener()
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}

export function useSpeakReplies(): boolean {
  return useSyncExternalStore(subscribe, isSpeakRepliesOn, () => false)
}
