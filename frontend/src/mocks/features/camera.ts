import { HttpResponse, http } from 'msw'
import type { CameraFeed, PrinterCamera } from '../../api/types'
import { printersNowState } from './printersNow'

/**
 * #7330 — a printer's recorded camera and its live view. A card with `camera_live` is
 * recorded, with both feeds receiving. The mocks have no live edge: the WHEP offer is
 * refused as the edge's 502, so the player falls back to LL-HLS at once, as on a network
 * where WebRTC's UDP is blocked. The HLS playlist is live and has no segment yet: hls.js
 * gives up on it, the player asks again each second, and says "Camera unavailable" when
 * its 10 s wait ends.
 */
const PROBLEMS = 'https://scadbuddy.dev/problems/'

const RECEIVING: CameraFeed[] = [
  { part: 'relay', state: 'pulling', detail: null, updated_at: '2026-11-25T12:00:00Z' },
  { part: 'recorder', state: 'pulling', detail: null, updated_at: '2026-11-25T12:00:00Z' },
]

const state: { feeds: Map<string, CameraFeed[]>; reconnects: string[] } = { feeds: new Map(), reconnects: [] }

export function reset(): void {
  state.feeds = new Map()
  state.reconnects = []
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
  http.get('/api/v1/camera/live/:id/hls/:name', ({ params }) =>
    String(params.name).endsWith('.m3u8')
      ? new HttpResponse(PLAYLIST, {
          headers: { 'Content-Type': 'application/vnd.apple.mpegurl', 'Cache-Control': 'no-store' },
        })
      : new HttpResponse(null, { status: 404 }),
  ),
]
