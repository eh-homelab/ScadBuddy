import { HttpResponse, http } from 'msw'
import type { CameraFeed, PrinterCamera } from '../../api/types'
import { emitRealtime } from '../realtime'
import { printersNowState } from './printersNow'

/**
 * #7330 — a printer's recorded camera and its live view. A card with `camera_live` is
 * recorded, with both feeds receiving. The mocks have no live edge: the WHEP offer is
 * refused as the edge's 502, so the player falls back to LL-HLS at once, as on a network
 * where WebRTC's UDP is blocked. The HLS routes answer as MediaMTX v1.21.4 does through
 * the proxy (`backend/tests/api/test_camera_live.py`): `index.m3u8` with no cookie is a
 * 302 to `index.m3u8?cookieCheck=1`, which names its media playlist relative to itself
 * with the session in the `session` query key. The media playlist is live and has no
 * segment yet: hls.js
 * gives up on it, the player asks again each second, and says "Camera unavailable" when
 * its 10 s wait ends. A reconnect plays the relay's restart: the relay feed reports
 * `stopped`, then `pulling` again, each with a `camera.feed` event, as `Relay.restart` does.
 */
const PROBLEMS = 'https://scadbuddy.dev/problems/'

const RECEIVING: CameraFeed[] = [
  { part: 'relay', state: 'pulling', detail: null, updated_at: '2026-11-25T12:00:00Z' },
  { part: 'recorder', state: 'pulling', detail: null, updated_at: '2026-11-25T12:00:00Z' },
]

const state: { feeds: Map<string, CameraFeed[]>; reconnects: string[]; generation: number } = {
  feeds: new Map(),
  reconnects: [],
  generation: 0,
}

export function reset(): void {
  state.feeds = new Map()
  state.reconnects = []
  // A restart still under way from the last test does not report into the next one.
  state.generation += 1
}

/** When the mocked relay reports `stopped`, then `pulling`, after a reconnect. */
const MOCK_RESTART_MS = { stopped: 200, pulling: 1500 }

function reportRelay(printerId: string, feedState: CameraFeed['state'], generation: number): void {
  if (generation !== state.generation) return
  const others = (state.feeds.get(printerId) ?? RECEIVING).filter((feed) => feed.part !== 'relay')
  const relay: CameraFeed = { part: 'relay', state: feedState, detail: null, updated_at: new Date().toISOString() }
  state.feeds.set(printerId, [relay, ...others])
  emitRealtime('camera.feed', ['printers', `printer:${printerId}`], { printer_id: printerId })
}

/** Replace a printer's feed states, for a test. */
export function setCameraFeeds(printerId: string, feeds: CameraFeed[]): void {
  state.feeds.set(printerId, feeds)
}

/** The printers a reconnect was asked for, in order. */
export function cameraReconnects(): string[] {
  return [...state.reconnects]
}

function recorded(printerId: string): boolean {
  return printersNowState().some((card) => card.id === printerId && card.camera_live)
}

function notRecorded(printerId: string) {
  return HttpResponse.json(
    {
      type: `${PROBLEMS}camera-not-recorded`,
      title: 'Conflict',
      status: 409,
      detail: `printer ${printerId} is not recorded`,
    },
    { status: 409 },
  )
}

/** The session the mocked edge names in its relative URIs. */
export const MOCK_HLS_SESSION = 'mock-session'

/** The multivariant playlist, after the cookie check. */
const INDEX = [
  '#EXTM3U',
  '#EXT-X-VERSION:9',
  '#EXT-X-INDEPENDENT-SEGMENTS',
  '',
  '#EXT-X-STREAM-INF:BANDWIDTH=1200000,CODECS="avc1.64001f",RESOLUTION=1280x720,FRAME-RATE=30.000',
  `video1_stream.m3u8?session=${MOCK_HLS_SESSION}`,
  '',
].join('\n')

/** A live LL-HLS media playlist before its first segment. */
const PLAYLIST = [
  '#EXTM3U',
  '#EXT-X-VERSION:9',
  '#EXT-X-TARGETDURATION:1',
  '#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=0.6',
  '#EXT-X-PART-INF:PART-TARGET=0.2',
  '#EXT-X-MEDIA-SEQUENCE:0',
  '',
].join('\n')

export const handlers = [
  http.get('/api/v1/printers/:id/camera', ({ params }) => {
    const id = String(params.id)
    const live = recorded(id)
    return HttpResponse.json({
      recorded: live,
      live,
      feeds: live ? (state.feeds.get(id) ?? RECEIVING) : [],
    } satisfies PrinterCamera)
  }),
  http.post('/api/v1/printers/:id/camera/reconnect', ({ params }) => {
    const id = String(params.id)
    if (!recorded(id)) return notRecorded(id)
    state.reconnects.push(id)
    const generation = state.generation
    emitRealtime('camera.reconnect', ['printers', `printer:${id}`], { printer_id: id })
    setTimeout(() => reportRelay(id, 'stopped', generation), MOCK_RESTART_MS.stopped)
    setTimeout(() => reportRelay(id, 'pulling', generation), MOCK_RESTART_MS.pulling)
    return HttpResponse.json({ printer_id: id }, { status: 202 })
  }),
  http.post('/api/v1/camera/live/:id/whep', () =>
    HttpResponse.json(
      {
        type: `${PROBLEMS}camera-edge-unreachable`,
        title: 'Bad Gateway',
        status: 502,
        detail: 'The live camera edge did not answer.',
      },
      { status: 502 },
    ),
  ),
  http.delete('/api/v1/camera/live/:id/whep/:session', () => new HttpResponse(null, { status: 200 })),
  http.get('/api/v1/camera/live/:id/hls/:name', ({ params, request }) => {
    const query = new URL(request.url).searchParams
    const headers = { 'Content-Type': 'application/vnd.apple.mpegurl', 'Cache-Control': 'no-store' }
    if (params.name === 'index.m3u8') {
      if (query.get('cookieCheck') !== '1') {
        return new HttpResponse(null, { status: 302, headers: { Location: 'index.m3u8?cookieCheck=1' } })
      }
      return new HttpResponse(INDEX, { headers })
    }
    if (params.name === 'video1_stream.m3u8' && query.get('session') === MOCK_HLS_SESSION) {
      return new HttpResponse(PLAYLIST, { headers })
    }
    return new HttpResponse(null, { status: 404 })
  }),
]
