import { useEffect, useRef, useState } from 'react'
import type Hls from 'hls.js'
import { LoaderCircleIcon, RotateCwIcon, VideoOffIcon } from 'lucide-react'
import { api } from '../../api/client'
import { hlsLoader } from './hlsLoader'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

/** Where the player is: trying WebRTC, playing over WebRTC or LL-HLS, or given up. */
export type LiveState = 'connecting' | 'webrtc' | 'hls' | 'failed'

/** How long WebRTC has to connect before the player switches to LL-HLS (#7330). */
export const WEBRTC_WAIT_MS = 6_000
/** How long LL-HLS has to show a first frame before the player gives up. */
export const HLS_WAIT_MS = 10_000
/** The pause before LL-HLS is asked again after hls.js gave up before a first frame. */
export const HLS_RETRY_MS = 1_000
/** How long the browser gathers its ICE candidates before the offer goes without the rest. */
const GATHER_WAIT_MS = 1_000

const BADGE: Partial<Record<LiveState, string>> = { webrtc: 'WebRTC', hls: 'HLS' }

/**
 * #7330 — a printer's live camera, from the live edge through ScadBuddy's origin
 * (`api/camera.py`). WebRTC (WHEP) first; when ICE has not connected in
 * {@link WEBRTC_WAIT_MS}, or the offer fails, it ends that session and plays LL-HLS with
 * hls.js (or the browser's own HLS where hls.js cannot run). LL-HLS with no frame in
 * {@link HLS_WAIT_MS} is `failed`. Unmounting, or another printer, ends the WHEP session
 * and stops hls.js, so the edge stops pulling a camera nobody watches.
 */
export function LivePlayer({
  printerId,
  onState,
  className,
}: {
  printerId: string
  onState?: (state: LiveState) => void
  className?: string
}) {
  const video = useRef<HTMLVideoElement>(null)
  const [state, setState] = useState<LiveState>('connecting')
  const [showing, setShowing] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const reported = useRef(onState)
  useEffect(() => {
    reported.current = onState
  })

  useEffect(() => {
    const element = video.current
    if (!element) return
    const base = api.cameraLiveUrl(printerId)
    let over = false
    let peer: RTCPeerConnection | null = null
    let session: string | null = null
    let hls: Hls | null = null
    let framed = false
    /** Bumped by each HLS start and by `fail`: an hls.js load for an older one starts nothing. */
    let hlsAttempt = 0
    const timers = new Set<ReturnType<typeof setTimeout>>()
    const later = (ms: number, run: () => void) => {
      const timer = setTimeout(() => {
        timers.delete(timer)
        run()
      }, ms)
      timers.add(timer)
      return timer
    }
    const clearTimers = () => {
      for (const timer of timers) clearTimeout(timer)
      timers.clear()
    }
    const report = (next: LiveState) => {
      if (over) return
      setState(next)
      reported.current?.(next)
    }
    report('connecting')
    setShowing(false)

    const endSession = () => {
      if (session === null) return
      // keepalive: the DELETE still goes when the dialog closes with the page.
      void fetch(session, { method: 'DELETE', keepalive: true }).catch(() => undefined)
      session = null
    }
    const endWebRtc = () => {
      // An offer still in flight is not aborted: its late answer ends its own session.
      if (peer) {
        peer.oniceconnectionstatechange = null
        peer.ontrack = null
        peer.close()
        peer = null
      }
      endSession()
      element.srcObject = null
    }
    const endHls = () => {
      hls?.destroy()
      hls = null
      if (element.hasAttribute('src')) {
        // The browser's own HLS: without `load()` it keeps the old source's reads.
        element.removeAttribute('src')
        element.load()
      }
    }
    const fail = () => {
      if (over) return
      hlsAttempt++
      clearTimers()
      endWebRtc()
      endHls()
      setShowing(false)
      report('failed')
    }
    const onFrame = () => {
      framed = true
      setShowing(true)
      clearTimers()
    }
    const onMediaError = () => {
      if (hls === null && element.hasAttribute('src')) fail()
    }
    element.addEventListener('loadeddata', onFrame)
    element.addEventListener('error', onMediaError)

    /** LL-HLS, with a new wait for a first frame unless `retry` keeps the running one. */
    const startHls = async (retry = false) => {
      if (over) return
      const mine = ++hlsAttempt
      if (!retry) {
        clearTimers()
        later(HLS_WAIT_MS, fail)
      }
      endWebRtc()
      endHls()
      framed = false
      setShowing(false)
      report('hls')
      const source = `${base}/hls/index.m3u8`
      let Player: typeof Hls
      try {
        Player = (await hlsLoader.load()).default
      } catch {
        if (!over && mine === hlsAttempt) fail()
        return
      }
      if (over || mine !== hlsAttempt) return
      if (Player.isSupported()) {
        const player = new Player({ lowLatencyMode: true })
        hls = player
        player.on(Player.Events.ERROR, (_event, data) => {
          if (!data.fatal || hls !== player) return
          // A session the edge ended (`camera-live-session-ended`) or a stall after
          // frames came: a new session, with a new wait. Before any frame (the edge's
          // on-demand pull still starting): again shortly, within the same wait.
          if (framed) {
            void startHls()
          } else {
            endHls()
            later(HLS_RETRY_MS, () => void startHls(true))
          }
        })
        player.loadSource(source)
        player.attachMedia(element)
      } else if (element.canPlayType('application/vnd.apple.mpegurl')) {
        element.src = source
      } else {
        fail()
      }
    }

    const startWebRtc = async () => {
      const connection = new RTCPeerConnection()
      peer = connection
      later(WEBRTC_WAIT_MS, () => void startHls())
      connection.addTransceiver('video', { direction: 'recvonly' })
      connection.ontrack = (event) => {
        element.srcObject = event.streams[0] ?? new MediaStream([event.track])
      }
      const current = () => !over && peer === connection
      const connected = () => ['connected', 'completed'].includes(connection.iceConnectionState)
      connection.oniceconnectionstatechange = () => {
        const ice = connection.iceConnectionState
        if (connected()) {
          clearTimers()
          report('webrtc')
        } else if (ice === 'failed') {
          void startHls()
        } else if (ice === 'disconnected') {
          // It may come back; a frozen frame under "WebRTC" for longer it must not show.
          later(WEBRTC_WAIT_MS, () => {
            if (current() && !connected()) void startHls()
          })
        }
      }
      try {
        await connection.setLocalDescription(await connection.createOffer())
        await gathered(connection)
        if (!current()) return
        const response = await fetch(`${base}/whep`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/sdp' },
          body: connection.localDescription?.sdp ?? '',
        })
        const location = response.headers.get('Location')
        if (response.ok && location) session = location
        if (!current()) {
          // The answer came after the fallback, the close or another printer.
          endSession()
          return
        }
        if (!response.ok || !location) throw new Error(`WHEP answered ${response.status}`)
        await connection.setRemoteDescription({ type: 'answer', sdp: await response.text() })
      } catch {
        if (current()) void startHls()
      }
    }

    if (typeof RTCPeerConnection === 'undefined') {
      void startHls()
    } else {
      void startWebRtc()
    }

    return () => {
      clearTimers()
      endWebRtc()
      endHls()
      over = true
      element.removeEventListener('loadeddata', onFrame)
      element.removeEventListener('error', onMediaError)
    }
  }, [printerId, attempt])

  const badge = BADGE[state]
  return (
    <div
      className={cn('relative aspect-video w-full overflow-hidden rounded-lg border bg-muted', className)}
      data-testid="live-player"
      data-state={state}
    >
      <video ref={video} muted playsInline autoPlay className="size-full object-contain" />
      {badge && (
        <span className="absolute top-2 left-2 rounded bg-background/80 px-1.5 py-0.5 text-[10.5px] font-medium text-muted-foreground backdrop-blur-sm">
          {badge}
        </span>
      )}
      {state === 'failed' ? (
        <div
          className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-4 text-center"
          role="status"
        >
          <VideoOffIcon aria-hidden="true" className="size-6 text-muted-foreground" />
          <p className="text-sm font-medium">Camera unavailable</p>
          <p className="max-w-xs text-xs text-muted-foreground">
            No video came from the camera. The feed states beside it say where it stopped.
          </p>
          <Button variant="outline" size="sm" onClick={() => setAttempt((count) => count + 1)}>
            <RotateCwIcon aria-hidden="true" />
            Try again
          </Button>
        </div>
      ) : (
        !showing && (
          <div className="absolute inset-0 flex items-center justify-center gap-2 text-xs text-muted-foreground" role="status">
            <LoaderCircleIcon aria-hidden="true" className="size-4 motion-safe:animate-spin" />
            Connecting
          </div>
        )
      )}
    </div>
  )
}

/** Resolves once the browser has gathered its ICE candidates, or after {@link GATHER_WAIT_MS}. */
function gathered(connection: RTCPeerConnection): Promise<void> {
  if (connection.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer)
      connection.removeEventListener('icegatheringstatechange', check)
      resolve()
    }
    const check = () => {
      if (connection.iceGatheringState === 'complete') done()
    }
    const timer = setTimeout(done, GATHER_WAIT_MS)
    connection.addEventListener('icegatheringstatechange', check)
  })
}
