import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { server } from '../../mocks/server'
import { hlsLoader } from './hlsLoader'
import { HLS_RETRY_MS, HLS_WAIT_MS, LivePlayer, type LiveState, WEBRTC_WAIT_MS } from './LivePlayer'

/** A fake hls.js: what the player asked of each instance. */
const hlsFake = vi.hoisted(() => ({
  supported: true,
  instances: [] as {
    source?: string
    media?: HTMLMediaElement
    destroyed: boolean
    handlers: Map<string, (event: string, data: unknown) => void>
  }[],
}))

vi.mock('hls.js', () => {
  class FakeHls {
    static isSupported = () => hlsFake.supported
    static Events = { ERROR: 'hlsError' }
    source?: string
    media?: HTMLMediaElement
    destroyed = false
    handlers = new Map<string, (event: string, data: unknown) => void>()
    constructor() {
      hlsFake.instances.push(this)
    }
    loadSource(url: string) {
      this.source = url
    }
    attachMedia(media: HTMLMediaElement) {
      this.media = media
    }
    on(event: string, handler: (event: string, data: unknown) => void) {
      this.handlers.set(event, handler)
    }
    destroy() {
      this.destroyed = true
    }
  }
  return { default: FakeHls }
})

/** A fake `RTCPeerConnection` that never connects unless a test says so. */
class FakePeer {
  static all: FakePeer[] = []
  iceConnectionState: RTCIceConnectionState = 'new'
  iceGatheringState: RTCIceGatheringState = 'complete'
  localDescription: RTCSessionDescriptionInit | null = null
  remote: RTCSessionDescriptionInit | null = null
  closed = false
  kinds: string[] = []
  oniceconnectionstatechange: (() => void) | null = null
  ontrack: ((event: RTCTrackEvent) => void) | null = null
  constructor() {
    FakePeer.all.push(this)
  }
  addTransceiver(kind: string, init: RTCRtpTransceiverInit) {
    this.kinds.push(`${kind}:${init.direction}`)
  }
  createOffer() {
    return Promise.resolve({ type: 'offer' as const, sdp: 'v=0\r\nfake-offer\r\n' })
  }
  setLocalDescription(description: RTCSessionDescriptionInit) {
    this.localDescription = description
    return Promise.resolve()
  }
  setRemoteDescription(description: RTCSessionDescriptionInit) {
    this.remote = description
    return Promise.resolve()
  }
  addEventListener() {}
  removeEventListener() {}
  close() {
    this.closed = true
  }
  connect() {
    this.iceConnectionState = 'connected'
    this.oniceconnectionstatechange?.()
  }
}

const BASE = '/api/v1/camera/live/p1'

interface Seen {
  offers: string[]
  deleted: string[]
}

function edge(answer: 'ok' | 'down' = 'ok', hold?: Promise<void>): Seen {
  const seen: Seen = { offers: [], deleted: [] }
  let next = 0
  server.use(
    http.post('/api/v1/camera/live/:id/whep', async ({ request, params }) => {
      seen.offers.push(await request.text())
      await hold
      if (answer === 'down') {
        return HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/camera-edge-unreachable', title: 'Bad Gateway', status: 502 },
          { status: 502 },
        )
      }
      return new HttpResponse('v=0\r\nfake-answer\r\n', {
        status: 201,
        headers: {
          'Content-Type': 'application/sdp',
          Location: `/api/v1/camera/live/${String(params.id)}/whep/session-${++next}`,
        },
      })
    }),
    http.delete('/api/v1/camera/live/:id/whep/:session', ({ request }) => {
      seen.deleted.push(new URL(request.url).pathname)
      return new HttpResponse(null, { status: 200 })
    }),
  )
  return seen
}

function renderPlayer(printerId = 'p1') {
  const states: LiveState[] = []
  const view = render(<LivePlayer printerId={printerId} onState={(state) => states.push(state)} />)
  return { states, view }
}

beforeEach(() => {
  FakePeer.all = []
  hlsFake.instances = []
  hlsFake.supported = true
  vi.stubGlobal('RTCPeerConnection', FakePeer)
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('LivePlayer (#7330)', () => {
  it('offers one receive-only video track and plays over WebRTC once ICE connects', async () => {
    const seen = edge()
    const { states } = renderPlayer()
    await waitFor(() => expect(FakePeer.all[0]?.remote?.sdp).toContain('fake-answer'))
    const peer = FakePeer.all[0]!
    expect(peer.kinds).toEqual(['video:recvonly'])
    expect(seen.offers).toEqual(['v=0\r\nfake-offer\r\n'])
    act(() => peer.connect())
    expect(states.at(-1)).toBe('webrtc')
    expect(screen.getByText('WebRTC')).toBeInTheDocument()
    // Connected is not yet a picture: "Connecting" stays until the first frame.
    expect(screen.getByText('Connecting')).toBeInTheDocument()
    fireEvent.loadedData(document.querySelector('video')!)
    expect(screen.queryByText('Connecting')).toBeNull()
    // Connected in time: no fallback after the wait.
    await act(() => vi.advanceTimersByTimeAsync(WEBRTC_WAIT_MS + 1))
    expect(hlsFake.instances).toHaveLength(0)
  })

  it('falls back to HLS when ICE has not connected after 6 s, and ends the WHEP session', async () => {
    const seen = edge()
    const { states } = renderPlayer()
    await waitFor(() => expect(FakePeer.all[0]?.remote).not.toBeNull())
    expect(states.at(-1)).toBe('connecting')
    expect(hlsFake.instances).toHaveLength(0)

    await act(() => vi.advanceTimersByTimeAsync(WEBRTC_WAIT_MS))
    await waitFor(() => expect(hlsFake.instances).toHaveLength(1))
    expect(FakePeer.all[0]!.closed).toBe(true)
    await waitFor(() => expect(seen.deleted).toEqual([`${BASE}/whep/session-1`]))
    expect(hlsFake.instances[0]!.source).toBe(`${BASE}/hls/index.m3u8`)
    expect(hlsFake.instances[0]!.media).toBeInstanceOf(HTMLVideoElement)
    expect(states.at(-1)).toBe('hls')
    expect(screen.getByText('HLS')).toBeInTheDocument()
  })

  it('ends the session of an answer that comes after the fallback', async () => {
    let answer = () => {}
    const seen = edge('ok', new Promise<void>((resolve) => (answer = resolve)))
    const { states } = renderPlayer()
    await waitFor(() => expect(seen.offers).toHaveLength(1))
    await act(() => vi.advanceTimersByTimeAsync(WEBRTC_WAIT_MS))
    await waitFor(() => expect(hlsFake.instances).toHaveLength(1))
    expect(seen.deleted).toEqual([])

    answer()
    await waitFor(() => expect(seen.deleted).toEqual([`${BASE}/whep/session-1`]))
    expect(FakePeer.all[0]!.remote).toBeNull()
    expect(hlsFake.instances).toHaveLength(1)
    expect(states.at(-1)).toBe('hls')
  })

  it('falls back to HLS when ICE stays disconnected for 6 s', async () => {
    const seen = edge()
    const { states } = renderPlayer()
    await waitFor(() => expect(FakePeer.all[0]?.remote).not.toBeNull())
    const peer = FakePeer.all[0]!
    act(() => peer.connect())
    const drop = () => {
      peer.iceConnectionState = 'disconnected'
      peer.oniceconnectionstatechange?.()
    }

    // Back within the wait: no fallback.
    act(drop)
    await act(() => vi.advanceTimersByTimeAsync(WEBRTC_WAIT_MS - 1_000))
    act(() => peer.connect())
    await act(() => vi.advanceTimersByTimeAsync(2_000))
    expect(hlsFake.instances).toHaveLength(0)
    expect(states.at(-1)).toBe('webrtc')

    act(drop)
    await act(() => vi.advanceTimersByTimeAsync(WEBRTC_WAIT_MS))
    await waitFor(() => expect(hlsFake.instances).toHaveLength(1))
    expect(peer.closed).toBe(true)
    await waitFor(() => expect(seen.deleted).toEqual([`${BASE}/whep/session-1`]))
    expect(states.at(-1)).toBe('hls')
  })

  it('starts no hls.js when the wait ended while hls.js was still loading', async () => {
    edge('down')
    let loaded = () => {}
    const real = hlsLoader.load
    const late = new Promise<void>((resolve) => (loaded = resolve))
    const load = vi.spyOn(hlsLoader, 'load').mockImplementation(async () => {
      await late
      return real()
    })
    const { states } = renderPlayer()
    await waitFor(() => expect(load).toHaveBeenCalled())
    await act(() => vi.advanceTimersByTimeAsync(HLS_WAIT_MS))
    expect(states.at(-1)).toBe('failed')

    loaded()
    await act(() => vi.advanceTimersByTimeAsync(10))
    expect(hlsFake.instances).toHaveLength(0)
    expect(states.at(-1)).toBe('failed')
    load.mockRestore()
  })

  it('falls back to HLS at once when the offer is refused', async () => {
    edge('down')
    const { states } = renderPlayer()
    await waitFor(() => expect(hlsFake.instances).toHaveLength(1))
    expect(FakePeer.all[0]!.closed).toBe(true)
    expect(states.at(-1)).toBe('hls')
  })

  it('goes straight to HLS in a browser without WebRTC', async () => {
    const seen = edge()
    vi.stubGlobal('RTCPeerConnection', undefined)
    renderPlayer()
    await waitFor(() => expect(hlsFake.instances).toHaveLength(1))
    expect(seen.offers).toEqual([])
  })

  it('is failed when HLS shows no frame in 10 s, and offers to try again', async () => {
    edge('down')
    const { states } = renderPlayer()
    await waitFor(() => expect(hlsFake.instances).toHaveLength(1))
    await act(() => vi.advanceTimersByTimeAsync(HLS_WAIT_MS))
    expect(states.at(-1)).toBe('failed')
    expect(screen.getByText('Camera unavailable')).toBeInTheDocument()
    expect(hlsFake.instances[0]!.destroyed).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(FakePeer.all).toHaveLength(2))
    expect(states.slice(states.lastIndexOf('failed'))).toContain('connecting')
  })

  it('keeps playing HLS once a frame arrives', async () => {
    edge('down')
    const { states, view } = renderPlayer()
    await waitFor(() => expect(hlsFake.instances).toHaveLength(1))
    fireEvent.loadedData(view.container.querySelector('video')!)
    await act(() => vi.advanceTimersByTimeAsync(HLS_WAIT_MS + 1))
    expect(states.at(-1)).toBe('hls')
  })

  it('retries HLS after a fatal error before any frame, within the same 10 s wait', async () => {
    edge('down')
    const { states } = renderPlayer()
    await waitFor(() => expect(hlsFake.instances).toHaveLength(1))
    act(() => hlsFake.instances[0]!.handlers.get('hlsError')!('hlsError', { fatal: true, type: 'networkError' }))
    expect(hlsFake.instances[0]!.destroyed).toBe(true)
    await act(() => vi.advanceTimersByTimeAsync(HLS_RETRY_MS))
    await waitFor(() => expect(hlsFake.instances).toHaveLength(2))
    expect(states.at(-1)).toBe('hls')
    await act(() => vi.advanceTimersByTimeAsync(HLS_WAIT_MS - HLS_RETRY_MS))
    expect(states.at(-1)).toBe('failed')
    expect(hlsFake.instances[1]!.destroyed).toBe(true)
  })

  it('starts a new HLS session on a fatal error after frames came (the edge ended the session)', async () => {
    edge('down')
    const { states, view } = renderPlayer()
    await waitFor(() => expect(hlsFake.instances).toHaveLength(1))
    fireEvent.loadedData(view.container.querySelector('video')!)
    act(() => hlsFake.instances[0]!.handlers.get('hlsError')!('hlsError', { fatal: true, type: 'networkError' }))
    await waitFor(() => expect(hlsFake.instances).toHaveLength(2))
    expect(hlsFake.instances[0]!.destroyed).toBe(true)
    expect(states.at(-1)).toBe('hls')
  })

  it('ends the WHEP session and closes the peer on unmount', async () => {
    const seen = edge()
    const { view } = renderPlayer()
    await waitFor(() => expect(FakePeer.all[0]?.remote).not.toBeNull())
    act(() => FakePeer.all[0]!.connect())
    view.unmount()
    expect(FakePeer.all[0]!.closed).toBe(true)
    await waitFor(() => expect(seen.deleted).toEqual([`${BASE}/whep/session-1`]))
  })

  it('stops hls.js on unmount', async () => {
    edge('down')
    const { view } = renderPlayer()
    await waitFor(() => expect(hlsFake.instances).toHaveLength(1))
    view.unmount()
    expect(hlsFake.instances[0]!.destroyed).toBe(true)
  })

  it("ends the last printer's session when the printer changes", async () => {
    const seen = edge()
    const { view } = renderPlayer('p1')
    await waitFor(() => expect(FakePeer.all[0]?.remote).not.toBeNull())
    view.rerender(<LivePlayer printerId="p2" />)
    await waitFor(() => expect(seen.deleted).toEqual([`${BASE}/whep/session-1`]))
    expect(FakePeer.all[0]!.closed).toBe(true)
    await waitFor(() => expect(seen.offers).toHaveLength(2))
  })
})
