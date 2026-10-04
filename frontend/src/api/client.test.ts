import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BUILTIN_SLUG, GALLERY_SLUG, media } from '../mocks/fixtures'
import { server } from '../mocks/server'
import {
  ApiError,
  BAMBUDDY_UNAVAILABLE,
  UNANSWERED,
  api,
  mayHaveRun,
  newRequestId,
  printRunPoll,
} from './client'
import type { MediaView } from './types'

const video = media[GALLERY_SLUG]!.find((item) => item.kind === 'video')!
const picture = media[GALLERY_SLUG]![0]!

describe('printRunPoll still-accepting budget (#1061)', () => {
  it('is the backend CLIENT_ACCEPTING (printing.py), as the agent ACCEPTING_MS is', () => {
    expect(printRunPoll.acceptingMs).toBe(240_000)
  })
})

describe('media URLs (#274)', () => {
  it('addresses an item by its id, with the slug encoded', () => {
    expect(api.mediaUrl(BUILTIN_SLUG, picture)).toBe(
      `/api/v1/models/builtin%3Akeychain-template/media/${picture.id}`,
    )
  })

  it('gives a video its poster URL, and an item with no poster none', () => {
    expect(api.mediaPosterUrl('name-keychain', video)).toBe(
      `/api/v1/models/name-keychain/media/${video.id}/poster`,
    )
    expect(api.mediaPosterUrl('name-keychain', picture)).toBeUndefined()
  })
})

/** Just enough of an `XMLHttpRequest` to see what `uploadMedia` sends and to answer it. */
class FakeXhr {
  static last: FakeXhr | undefined
  method = ''
  url = ''
  body: FormData | undefined
  headers: Record<string, string> = {}
  status = 0
  statusText = ''
  responseText = ''
  responseHeaders: Record<string, string> = {}
  readonly upload = new EventTarget()
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  onabort: (() => void) | null = null

  constructor() {
    FakeXhr.last = this
  }
  open(method: string, url: string) {
    this.method = method
    this.url = url
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value
  }
  getResponseHeader(name: string): string | null {
    return this.responseHeaders[name] ?? null
  }
  send(body: FormData) {
    this.body = body
  }
  progress(loaded: number, total: number) {
    this.upload.dispatchEvent(
      Object.assign(new Event('progress'), { lengthComputable: true, loaded, total }),
    )
  }
  answer(status: number, body: unknown, statusText = '') {
    this.status = status
    this.statusText = statusText
    this.responseText = JSON.stringify(body)
    this.onload?.()
  }
}

describe('uploadMedia (#274)', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    FakeXhr.last = undefined
  })

  function upload(
    options: { caption?: string; poster?: File } = {},
    onProgress?: (fraction: number) => void,
  ) {
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    const file = new File([new Uint8Array([0x89, 0x50])], 'clip.mp4')
    const pending = api.uploadMedia(BUILTIN_SLUG, file, options, onProgress)
    return { pending, xhr: FakeXhr.last!, file }
  }

  it('posts the file, caption and poster as multipart, and reports progress', async () => {
    const poster = new File([new Uint8Array([1])], 'poster.png')
    const seen: number[] = []
    const { pending, xhr, file } = upload({ caption: 'A clip', poster }, (f) => seen.push(f))

    expect(xhr.method).toBe('POST')
    expect(xhr.url).toBe('/api/v1/models/builtin%3Akeychain-template/media')
    expect(xhr.headers['Accept']).toBe('application/json')
    expect((xhr.body!.get('file') as File).name).toBe(file.name)
    expect(xhr.body!.get('caption')).toBe('A clip')
    expect((xhr.body!.get('poster') as File).name).toBe('poster.png')

    xhr.progress(25, 100)
    xhr.progress(100, 100)
    expect(seen).toEqual([0.25, 1])

    const record = { slug: 'x', media: [] as MediaView[] }
    xhr.answer(200, record)
    await expect(pending).resolves.toEqual(record)
  })

  it('leaves out the parts that were not given', () => {
    const { xhr } = upload()
    expect(xhr.body!.has('caption')).toBe(false)
    expect(xhr.body!.has('poster')).toBe(false)
  })

  it('rejects with the problem the server answered', async () => {
    const { pending, xhr } = upload()
    xhr.answer(413, {
      title: 'Content Too Large',
      status: 413,
      detail: 'a media upload is at most 1024 MB (SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES), and this one is larger',
    })
    const error = await pending.catch((cause: unknown) => cause)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(413)
    expect((error as ApiError).detail).toContain('at most 1024 MB')
  })

  it("keeps a 503's Retry-After as retry_after, as the fetch path does (#1000)", async () => {
    const { pending, xhr } = upload()
    xhr.responseHeaders['Retry-After'] = '30'
    xhr.answer(503, { title: 'Service Unavailable', status: 503, detail: 'the render queue is full' })
    await expect(pending).rejects.toMatchObject({ status: 503, problem: { retry_after: 30 } })
  })

  it('rejects with what the status means when the answer is not a problem', async () => {
    const { pending, xhr } = upload()
    xhr.status = 502
    xhr.statusText = 'Bad Gateway'
    xhr.responseText = '<html>'
    xhr.onload?.()
    await expect(pending).rejects.toMatchObject({
      status: 502,
      detail: 'The server is not answering right now (HTTP 502).',
      problem: { title: 'Bad Gateway' },
    })
  })

  it('rejects when the connection fails', async () => {
    const { pending, xhr } = upload()
    xhr.onerror?.()
    await expect(pending).rejects.toThrow('The upload failed')
    // Like a dropped fetch: no answer arrived, so the upload may have landed.
    expect(mayHaveRun(await pending.catch((caught: unknown) => caught))).toBe(true)
  })
})

describe('media writes against the mock API (#274)', () => {
  it('captions, reorders and deletes, each answering the model', async () => {
    const [first, second, third, fourth] = media[GALLERY_SLUG]!.map((item) => item.id)
    const copy = await api.duplicateModel(GALLERY_SLUG, 'Gallery')

    const captioned = await api.patchMedia(copy.slug, first!, 'The cover')
    expect(captioned.media?.[0]?.caption).toBe('The cover')

    const reordered = await api.reorderMedia(copy.slug, [fourth!, third!, second!, first!])
    expect(reordered.media?.map((item) => item.id)).toEqual([fourth, third, second, first])

    const deleted = await api.deleteMedia(copy.slug, third!)
    expect(deleted.media?.map((item) => item.id)).toEqual([fourth, second, first])
  })
})

describe('runPrint follows the run the server answers with 202 (#470)', () => {
  const body = { choices: { nozzles: [], tier: 'standard' } } as unknown as Parameters<
    typeof api.runPrint
  >[1]
  const started = {
    id: 'run-1',
    output_id: 'out-1',
    status: 'running',
    created_at: '2026-09-28T10:00:00Z',
    finished_at: null,
    result: null,
    error: null,
  }

  afterEach(() => {
    server.resetHandlers()
    printRunPoll.intervalMs = 1000
    printRunPoll.reattempts = 3
    printRunPoll.acceptingMs = 240_000
    printRunPoll.followMs = 3_600_000
  })

  it('stops following a run that never ends, saying to check before printing again', async () => {
    // Review #1061: a run whose execution is gone would otherwise spin the dialog forever.
    printRunPoll.intervalMs = 1
    printRunPoll.followMs = 30
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => HttpResponse.json(started, { status: 202 })),
      http.get('/api/v1/print/runs/run-1', () => HttpResponse.json(started)),
    )
    const failure = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect(failure).toBeInstanceOf(ApiError)
    expect((failure as ApiError).detail).toMatch(/still preparing this print/)
    expect((failure as ApiError).problem.may_have_queued).toBe(true)
  })

  it('reads the run until it succeeds and returns its result', async () => {
    printRunPoll.intervalMs = 1
    const result = { queue_item_ids: [7], warnings: [] }
    let reads = 0
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => HttpResponse.json(started, { status: 202 })),
      http.get('/api/v1/print/runs/run-1', () => {
        reads += 1
        return HttpResponse.json(
          reads < 2 ? started : { ...started, status: 'succeeded', result },
        )
      }),
    )

    await expect(api.runPrint('out-1', body)).resolves.toEqual(result)
    expect(reads).toBe(2)
  })

  // No Retry-After unless a test sets one: a real one is whole seconds (review #1061 4a).
  const stillAccepting = (retryAfter?: string) =>
    HttpResponse.json(
      {
        type: 'https://scadbuddy.dev/problems/command-still-accepting',
        title: 'Service Unavailable',
        status: 503,
        detail: 'ScadBuddy is still checking this print.',
      },
      { status: 503, headers: retryAfter ? { 'Retry-After': retryAfter } : {} },
    )

  it("waits the still-accepting answer's Retry-After before sending again (review #1061 4a)", async () => {
    printRunPoll.intervalMs = 1
    const result = { queue_item_ids: [7], warnings: [] }
    const sent: number[] = []
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => {
        sent.push(Date.now())
        return sent.length < 2 ? stillAccepting('0.2') : HttpResponse.json(started, { status: 202 })
      }),
      http.get('/api/v1/print/runs/run-1', () => HttpResponse.json({ ...started, status: 'succeeded', result })),
    )

    await expect(api.runPrint('out-1', body)).resolves.toEqual(result)
    expect(sent).toHaveLength(2)
    expect(sent[1]! - sent[0]!).toBeGreaterThanOrEqual(190)
  })

  it('keeps sending while still accepting, past the re-sends for an unanswered request (#1052)', async () => {
    printRunPoll.intervalMs = 1
    const result = { queue_item_ids: [7], warnings: [] }
    let posts = 0
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => {
        posts += 1
        return posts <= printRunPoll.reattempts + 2 ? stillAccepting() : HttpResponse.json(started, { status: 202 })
      }),
      http.get('/api/v1/print/runs/run-1', () => HttpResponse.json({ ...started, status: 'succeeded', result })),
    )

    await expect(api.runPrint('out-1', body)).resolves.toEqual(result)
  })

  it('says a print still being accepted when it gave up may have started (#1052)', async () => {
    printRunPoll.intervalMs = 1
    printRunPoll.acceptingMs = 20
    server.use(http.post('/api/v1/print/outputs/out-1/run', () => stillAccepting()))

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect(mayHaveRun(error)).toBe(true)
  })

  it('sends the same request again while the server is still accepting it (#1052)', async () => {
    printRunPoll.intervalMs = 1
    const result = { queue_item_ids: [7], warnings: [] }
    let posts = 0
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => {
        posts += 1
        return posts < 2 ? stillAccepting() : HttpResponse.json(started, { status: 202 })
      }),
      http.get('/api/v1/print/runs/run-1', () => HttpResponse.json({ ...started, status: 'succeeded', result })),
    )

    await expect(api.runPrint('out-1', body)).resolves.toEqual(result)
    expect(posts).toBe(2)
  })

  it("follows a library file's run the same way (#742)", async () => {
    printRunPoll.intervalMs = 1
    const result = { queue_item_ids: [8], warnings: [] }
    let reads = 0
    server.use(
      http.post('/api/v1/print/library/89/run', () =>
        HttpResponse.json({ ...started, output_id: 'library:89' }, { status: 202 }),
      ),
      http.get('/api/v1/print/runs/run-1', () => {
        reads += 1
        return HttpResponse.json(reads < 2 ? started : { ...started, status: 'succeeded', result })
      }),
    )

    await expect(api.runLibraryPrint(89, body)).resolves.toEqual(result)
    expect(reads).toBe(2)
  })

  it("throws the failed run's problem, as the route used to answer it", async () => {
    printRunPoll.intervalMs = 1
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => HttpResponse.json(started, { status: 202 })),
      http.get('/api/v1/print/runs/run-1', () =>
        HttpResponse.json({
          ...started,
          status: 'failed',
          error: {
            status: 502,
            title: 'Bad Gateway',
            detail: 'Bambuddy failed to slice the plate: no support',
            extensions: { slice_job_id: 9 },
          },
        }),
      ),
    )

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(502)
    expect((error as ApiError).message).toBe('Bambuddy failed to slice the plate: no support')
    expect((error as ApiError).problem).toMatchObject({ slice_job_id: 9 })
  })

  it('says a failed run that had tried to queue may be on the queue anyway', async () => {
    printRunPoll.intervalMs = 1
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => HttpResponse.json(started, { status: 202 })),
      http.get('/api/v1/print/runs/run-1', () =>
        HttpResponse.json({
          ...started,
          status: 'failed',
          may_have_queued: true,
          error: {
            type: 'https://scadbuddy.dev/problems/bambuddy-unavailable',
            status: 504,
            title: 'Gateway Timeout',
            detail: 'Bambuddy did not answer in time.',
            extensions: {},
          },
        }),
      ),
    )

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(504)
    // The dialog adds the queue advice; the detail is the backend's own.
    expect((error as ApiError).detail).toBe('Bambuddy did not answer in time.')
    expect((error as ApiError).problem).toMatchObject({
      type: 'https://scadbuddy.dev/problems/bambuddy-unavailable',
      status: 504,
      may_have_queued: true,
    })
    expect(mayHaveRun(error)).toBe(true)
  })

  it('does not count a failed run that never tried to queue, whatever its type', async () => {
    printRunPoll.intervalMs = 1
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => HttpResponse.json(started, { status: 202 })),
      http.get('/api/v1/print/runs/run-1', () =>
        HttpResponse.json({
          ...started,
          status: 'failed',
          may_have_queued: false,
          error: {
            type: BAMBUDDY_UNAVAILABLE,
            status: 504,
            title: 'Gateway Timeout',
            detail: 'could not reach Bambuddy to slice the plate: ReadTimeout',
            extensions: {},
          },
        }),
      ),
    )

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect((error as ApiError).status).toBe(504)
    expect(mayHaveRun(error)).toBe(false)
  })

  it('counts a run lost while queueing as maybe queued, from the flag', async () => {
    const lost =
      'ScadBuddy restarted while it was preparing this print, after it had started queueing it, ' +
      'so it cannot tell whether the print was queued.'
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () =>
        HttpResponse.json({
          ...started,
          status: 'failed',
          may_have_queued: true,
          repeated: true,
          error: { status: 500, title: 'Internal Server Error', detail: lost, extensions: {} },
        }),
      ),
    )

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect((error as ApiError).detail).toBe(lost)
    expect(mayHaveRun(error)).toBe(true)
  })

  it('re-sends the same request when its answer never arrived, and re-attaches to the run', async () => {
    printRunPoll.intervalMs = 1
    const result = { queue_item_ids: [7], warnings: [] }
    const sent: unknown[] = []
    let reads = 0
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', async ({ request }) => {
        sent.push(await request.json())
        return sent.length === 1 ? HttpResponse.error() : HttpResponse.json(started, { status: 202 })
      }),
      http.get('/api/v1/print/runs/run-1', () => {
        reads += 1
        // A proxy's own 504 page: not ScadBuddy's answer, so read again.
        if (reads === 1) return new HttpResponse('<html>504</html>', { status: 504 })
        return HttpResponse.json({ ...started, status: 'succeeded', result })
      }),
    )

    const press = { ...body, request_id: 'press-1' }
    await expect(api.runPrint('out-1', press)).resolves.toEqual(result)
    expect(sent).toEqual([press, press])
    expect(reads).toBe(2)
  })

  it("does not re-send a request ScadBuddy's server refused", async () => {
    printRunPoll.intervalMs = 1
    let posts = 0
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => {
        posts += 1
        return HttpResponse.json(
          { type: 'about:blank', title: 'Bad Gateway', status: 502, detail: 'Bambuddy refused the API key' },
          { status: 502, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
    )

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect((error as ApiError).detail).toBe('Bambuddy refused the API key')
    expect(posts).toBe(1)
  })

  it('stops reading the run once its signal is aborted', async () => {
    printRunPoll.intervalMs = 5
    const controller = new AbortController()
    let reads = 0
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => HttpResponse.json(started, { status: 202 })),
      http.get('/api/v1/print/runs/run-1', () => {
        reads += 1
        if (reads === 2) controller.abort()
        return HttpResponse.json(started)
      }),
    )

    const error = await api
      .runPrint('out-1', body, controller.signal)
      .catch((caught: unknown) => caught)
    expect(controller.signal.aborted).toBe(true)
    expect(error).toBe(controller.signal.reason)
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(reads).toBe(2)
  })

  it('gives up re-attaching after a few unanswered tries', async () => {
    printRunPoll.intervalMs = 1
    printRunPoll.reattempts = 2
    let posts = 0
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => {
        posts += 1
        return HttpResponse.error()
      }),
    )

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).problem).toMatchObject({ type: UNANSWERED, status: 0 })
    expect(mayHaveRun(error)).toBe(true)
    expect(posts).toBe(3)
  })
})

describe('newRequestId', () => {
  it('is a new 128-bit hex id each time', () => {
    const one = newRequestId()
    expect(one).toMatch(/^[0-9a-f]{32}$/)
    expect(newRequestId()).not.toBe(one)
  })
})

describe('failures the server did not describe (#470)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  /** A proxy in front of the backend answers with its own page, and HTTP/2 has no status text. */
  function proxy(status: number, body = '<html><body>upstream request timeout</body></html>') {
    server.use(
      http.get(
        '/api/v1/models',
        () => new HttpResponse(body, { status, headers: { 'Content-Type': 'text/html' } }),
      ),
    )
  }

  async function failure(): Promise<ApiError> {
    const caught = await api.listModels().catch((cause: unknown) => cause)
    expect(caught).toBeInstanceOf(ApiError)
    return caught as ApiError
  }

  it.each([
    [504, 'The server took too long to answer (HTTP 504).'],
    [524, 'The server took too long to answer (HTTP 524).'],
    [502, 'The server is not answering right now (HTTP 502).'],
    [503, 'The server is not answering right now (HTTP 503).'],
    [413, 'That is too large for the server to accept (HTTP 413).'],
    [500, 'The server hit an error it did not describe (HTTP 500).'],
    [404, 'The server refused the request without saying why (HTTP 404).'],
  ])('says what a non-JSON %i means, and keeps the status', async (status, detail) => {
    proxy(status)
    const error = await failure()
    expect(error.status).toBe(status)
    expect(error.detail).toBe(detail)
    expect(error.message).toBe(detail)
    expect(error.problem.title).not.toMatch(/^$|Request failed/)
  })

  it('treats JSON that is not a problem like a proxy page', async () => {
    proxy(504, '{"message":"timeout"}')
    expect((await failure()).detail).toBe('The server took too long to answer (HTTP 504).')
  })

  it('keeps a problem the backend wrote as it is', async () => {
    server.use(
      http.get('/api/v1/models', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Bad Gateway',
            status: 502,
            detail: 'Bambuddy did not answer in time.',
          },
          { status: 502, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const error = await failure()
    expect(error.detail).toBe('Bambuddy did not answer in time.')
    expect(error.problem.title).toBe('Bad Gateway')
    // Not the backend's "Bambuddy did not answer" type, so nothing says it may have run.
    expect(mayHaveRun(error)).toBe(false)
  })

  it.each([
    [504, 'could not reach Bambuddy to queue the print: ReadTimeout', true],
    [502, 'could not reach Bambuddy to queue the print: RemoteProtocolError', true],
    [409, 'Bambuddy refused the API key', false],
    // map_response's fallback: Bambuddy answered, so its "no" is not a maybe...
    [502, 'Bambuddy answered 500 when asked to queue the print', false, 500],
    [502, 'Bambuddy answered 400 when asked to queue the print', false, 400],
    // ...unless the answer is a proxy's in front of Bambuddy that gave up waiting.
    [502, 'Bambuddy answered 504 when asked to queue the print', true, 504],
    [502, 'Bambuddy answered 524 when asked to queue the print', true, 524],
  ])(
    'counts the backend’s bambuddy-unavailable %i as maybe having run: %s',
    async (status, detail, expected, bambuddy_status?: number) => {
      server.use(
        http.get('/api/v1/models', () =>
          HttpResponse.json(
            {
              type: BAMBUDDY_UNAVAILABLE,
              title: 'Bad Gateway',
              status,
              detail,
              ...(bambuddy_status === undefined ? {} : { bambuddy_status }),
            },
            { status, headers: { 'Content-Type': 'application/problem+json' } },
          ),
        ),
      )
      const error = await failure()
      expect(error.detail).toBe(detail)
      expect(mayHaveRun(error)).toBe(expected)
    },
  )

  it('keeps a detail-only body', async () => {
    server.use(
      http.get('/api/v1/models', () => HttpResponse.json({ detail: 'Not Found' }, { status: 404 })),
    )
    expect((await failure()).detail).toBe('Not Found')
  })

  it('says the server could not be reached when the connection fails', async () => {
    server.use(http.get('/api/v1/models', () => HttpResponse.error()))
    const error = await failure()
    expect(error.status).toBe(0)
    expect(error.detail).toBe(
      'ScadBuddy could not reach its server, or the connection dropped before it answered.',
    )
    expect(mayHaveRun(error)).toBe(true)
  })

  it('says the browser is offline when it is', async () => {
    vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false)
    server.use(http.get('/api/v1/models', () => HttpResponse.error()))
    const error = await failure()
    expect(error.status).toBe(0)
    expect(error.detail).toBe('This browser is offline, so ScadBuddy could not reach its server.')
    expect(mayHaveRun(error)).toBe(false)
  })

  it('does not call it offline when the browser went offline only while waiting', async () => {
    let online = true
    vi.spyOn(navigator, 'onLine', 'get').mockImplementation(() => online)
    server.use(
      http.get('/api/v1/models', () => {
        online = false
        return HttpResponse.error()
      }),
    )
    const error = await failure()
    expect(error.problem.type).not.toBe('urn:scadbuddy:offline')
    expect(mayHaveRun(error)).toBe(true)
  })

  it('passes an abort through as it is', async () => {
    const controller = new AbortController()
    controller.abort()
    const caught = await api
      .checkSource('cube(1);', undefined, controller.signal)
      .catch((cause: unknown) => cause)
    expect(caught).not.toBeInstanceOf(ApiError)
    expect((caught as Error).name).toBe('AbortError')
  })

  it.each([
    [502, true],
    [504, true],
    [524, true],
    [503, false],
    [413, false],
    [500, false],
  ])('counts a non-JSON %i as maybe having run: %s', async (status, expected) => {
    proxy(status)
    expect(mayHaveRun(await failure())).toBe(expected)
  })

  it('does not count an error the backend described, or one that is not an ApiError', () => {
    expect(mayHaveRun(new Error('boom'))).toBe(false)
    expect(mayHaveRun(new ApiError(504, 'Bambuddy did not answer in time.'))).toBe(false)
  })

  it('says what a non-JSON upload failure means', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    const pending = api.uploadMedia(BUILTIN_SLUG, new File([new Uint8Array([1])], 'clip.mp4'))
    const xhr = FakeXhr.last!
    xhr.status = 413
    xhr.responseText = '<html>'
    xhr.onload?.()
    await expect(pending).rejects.toMatchObject({
      status: 413,
      detail: 'That is too large for the server to accept (HTTP 413).',
    })
  })
})

describe('definition files (#185)', () => {
  it('reads a file beside the model and one in a pinned library', async () => {
    await expect(api.getDefinitionFile('name-keychain', { path: 'helper.scad' })).resolves.toContain(
      'module rounded_plate',
    )
    await expect(
      api.getDefinitionFile('name-keychain', { library: 'BOSL2', path: 'shapes3d.scad' }),
    ).resolves.toContain('module cuboid')
  })

  it('encodes each segment of the path, and keeps its slashes', async () => {
    let asked = ''
    server.use(
      http.get('/api/v1/models/:slug/files/*', ({ request }) => {
        asked = new URL(request.url).pathname
        return new HttpResponse('x', { headers: { 'Content-Type': 'text/plain' } })
      }),
    )
    await api.getDefinitionFile('builtin:keychain', { path: 'my parts/a#b.scad' })
    expect(asked).toBe('/api/v1/models/builtin%3Akeychain/files/my%20parts/a%23b.scad')
  })

  it('names the pinned commit a library file is from', async () => {
    let asked = ''
    server.use(
      http.get('/api/v1/models/:slug/libraries/:name/files/*', ({ request }) => {
        const url = new URL(request.url)
        asked = url.pathname + url.search
        return new HttpResponse('x', { headers: { 'Content-Type': 'text/plain' } })
      }),
    )
    await api.getDefinitionFile('name-keychain', { library: 'BOSL2', commit: 'ab12', path: 'std.scad' })
    expect(asked).toBe('/api/v1/models/name-keychain/libraries/BOSL2/files/std.scad?commit=ab12')
  })

  it('is an ApiError for a file that is not there', async () => {
    await expect(api.getDefinitionFile('name-keychain', { path: 'nope.scad' })).rejects.toMatchObject({
      status: 404,
    })
  })
})

describe('render and createOutput (spec 2026-09-27 §4.3)', () => {
  it('send inputs, not params', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('/api/v1/models/:slug/render', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(
          { job_id: 'a'.repeat(32), status_url: '/api/v1/jobs/' + 'a'.repeat(32) },
          { status: 202 },
        )
      }),
      http.post('/api/v1/models/:slug/outputs', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({}, { status: 201 })
      }),
    )
    await api.render('name-keychain', { params: { name: 'Hi' }, tab: 'lid' })
    await api.createOutput('name-keychain', 'a'.repeat(32), undefined, { params: {}, tab: 'lid' })
    await api.createOutput('name-keychain', 'a'.repeat(32))
    expect(bodies).toEqual([
      { inputs: { params: { name: 'Hi' }, tab: 'lid' }, version: null },
      { job_id: 'a'.repeat(32), name: null, inputs: { params: {}, tab: 'lid' } },
      { job_id: 'a'.repeat(32), name: null },
    ])
  })

  it('addresses a template UI file, pinned by revision when there is one', () => {
    expect(api.uiFileUrl(BUILTIN_SLUG, undefined, 'app/index.html')).toBe(
      '/api/v1/models/builtin%3Akeychain-template/ui/app/index.html',
    )
    expect(api.uiFileUrl('name-keychain', 'abc', 'a b.js')).toBe(
      '/api/v1/models/name-keychain/versions/abc/ui/a%20b.js',
    )
  })
})

describe('command() sends a key and follows an operation (#1053)', () => {
  afterEach(() => {
    printRunPoll.intervalMs = 1000
    printRunPoll.reattempts = 3
    printRunPoll.operationFollowMs = 900_000
  })

  const operation = {
    id: 'op-1',
    kind: 'reprint',
    subject: 'archive:35',
    status: 'running',
    created_at: '2026-10-03T00:00:00Z',
  }
  const again = { queue_item_id: 51, printer_id: 3, bambuddy_url: 'http://b/queue' }

  it('sends one Idempotency-Key and re-sends it after an unanswered answer', async () => {
    printRunPoll.intervalMs = 1
    const keys: (string | null)[] = []
    server.use(
      http.post('/api/v1/prints/35/reprint', ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return keys.length < 2
          ? new HttpResponse('<html>upstream timed out</html>', { status: 504 })
          : HttpResponse.json(again, { status: 201 })
      }),
    )

    await expect(api.reprint(35)).resolves.toEqual(again)
    expect(keys).toHaveLength(2)
    expect(keys[0]).toMatch(/^[0-9a-f]{32}$/)
    expect(keys[1]).toBe(keys[0])
  })

  it("follows a 202 to the operation's result", async () => {
    printRunPoll.intervalMs = 1
    let reads = 0
    server.use(
      http.post('/api/v1/prints/35/reprint', () => HttpResponse.json(operation, { status: 202 })),
      http.get('/api/v1/operations/op-1', () => {
        reads += 1
        return HttpResponse.json(
          reads < 2 ? operation : { ...operation, status: 'succeeded', result: again },
        )
      }),
    )

    await expect(api.reprint(35)).resolves.toEqual(again)
  })

  it('stops following an operation that never ends, naming it (review #1063)', async () => {
    printRunPoll.intervalMs = 1
    printRunPoll.operationFollowMs = 20
    server.use(
      http.post('/api/v1/prints/35/reprint', () => HttpResponse.json(operation, { status: 202 })),
      http.get('/api/v1/operations/op-1', () => HttpResponse.json(operation)),
    )

    const caught = await api.reprint(35).catch((e: unknown) => e)
    expect(caught).toBeInstanceOf(ApiError)
    expect((caught as ApiError).status).toBe(504)
    expect((caught as ApiError).problem.detail).toContain('op-1')
    expect((caught as ApiError).problem.detail).toContain('check before trying again')
  })

  it("turns a failed operation into the route's ApiError", async () => {
    printRunPoll.intervalMs = 1
    const error = { type: 'about:blank', status: 502, title: 'Bad Gateway', detail: 'Bambuddy said no', extensions: {} }
    server.use(
      http.post('/api/v1/prints/35/reprint', () => HttpResponse.json(operation, { status: 202 })),
      http.get('/api/v1/operations/op-1', () => HttpResponse.json({ ...operation, status: 'failed', error })),
    )

    const caught = await api.reprint(35).catch((e: unknown) => e)
    expect(caught).toBeInstanceOf(ApiError)
    expect((caught as ApiError).status).toBe(502)
    expect((caught as ApiError).problem.detail).toBe('Bambuddy said no')
  })

  it.each([
    ['sendOutput', () => api.sendOutput('out-1', { mode: 'library' }), '/api/v1/outputs/out-1/send'],
    ['createProject', () => api.createProject({ name: 'P' }), '/api/v1/print/projects'],
    ['fileIntoProject', () => api.fileIntoProject('out-1', 7), '/api/v1/outputs/out-1/project-file'],
    ['attachToProject', () => api.attachToProject('out-1', {}), '/api/v1/print/outputs/out-1/project'],
    ['pullTimelapse', () => api.pullTimelapse(35, 'a.mp4'), '/api/v1/prints/35/timelapse/pull'],
    ['registerSidebar', () => api.registerSidebar(), '/api/v1/settings/register-sidebar'],
  ])('%s sends an Idempotency-Key', async (_name, call, path) => {
    let key: string | null = null
    server.use(
      http.post(path, ({ request }) => {
        key = request.headers.get('Idempotency-Key')
        return HttpResponse.json({}, { status: 200 })
      }),
    )
    await call()
    expect(key).toMatch(/^[0-9a-f]{32}$/)
  })
})

describe('library pins are commands (#1054)', () => {
  const defaults = { ...printRunPoll }
  afterEach(() => {
    Object.assign(printRunPoll, defaults)
  })

  it.each([
    ['pinModelLibrary', () => api.pinModelLibrary('w', 'BOSL2', {}), 'put'],
    ['repinModelLibrary', () => api.repinModelLibrary('w', 'BOSL2', {}), 'patch'],
    ['unpinModelLibrary', () => api.unpinModelLibrary('w', 'BOSL2'), 'delete'],
  ] as const)('%s sends an Idempotency-Key and follows a 202 to the model', async (_name, call, method) => {
    printRunPoll.intervalMs = 1
    const model = { slug: 'w', name: 'W' }
    const operation = {
      id: 'op-9',
      kind: 'library_pin',
      subject: 'w',
      status: 'running',
      created_at: '2026-10-03T00:00:00Z',
    }
    let key: string | null = null
    server.use(
      http[method]('/api/v1/models/w/libraries/BOSL2', ({ request }) => {
        key = request.headers.get('Idempotency-Key')
        return HttpResponse.json(operation, { status: 202 })
      }),
      http.get('/api/v1/operations/op-9', () =>
        HttpResponse.json({ ...operation, status: 'succeeded', result: model }),
      ),
    )
    await expect(call()).resolves.toEqual(model)
    expect(key).toMatch(/^[0-9a-f]{32}$/)
  })

  it("rejects with a failed operation's problem, so the pin dialog's 409 still applies", async () => {
    printRunPoll.intervalMs = 1
    const operation = {
      id: 'op-9',
      kind: 'library_pin',
      subject: 'w',
      status: 'running',
      created_at: '2026-10-03T00:00:00Z',
    }
    const detail = "'w''s 'BOSL2' was changed or removed while this re-pin ran; nothing was recorded"
    server.use(
      http.put('/api/v1/models/w/libraries/BOSL2', () => HttpResponse.json(operation, { status: 202 })),
      http.get('/api/v1/operations/op-9', () =>
        HttpResponse.json({
          ...operation,
          status: 'failed',
          error: { status: 409, title: 'Conflict', detail, extensions: {} },
        }),
      ),
    )
    const error = await api.pinModelLibrary('w', 'BOSL2', {}).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(409)
    expect((error as ApiError).message).toBe(detail)
  })
})

describe("a model's lifecycle is commands (#1054)", () => {
  afterEach(() => {
    printRunPoll.intervalMs = 1000
  })

  const operation = { id: 'op-9', kind: 'model_create', subject: 'w', status: 'running', created_at: '2026-10-03T00:00:00Z' }

  it.each([
    ['createModelFromSource', () => api.createModelFromSource({ name: 'W', source: 'cube(1);', description: '', force: false }), 'post', '/api/v1/models'],
    ['importModel', () => api.importModel({ url: 'https://example.com/w.scad', force: false }), 'post', '/api/v1/models/import'],
    ['updateModel', () => api.updateModel('w', { description: 'd' }), 'patch', '/api/v1/models/w'],
    ['duplicateModel', () => api.duplicateModel('v', 'W'), 'post', '/api/v1/models/v/duplicate'],
  ] as const)('%s sends an Idempotency-Key and follows a 202 to the model', async (_name, call, method, path) => {
    printRunPoll.intervalMs = 1
    const model = { slug: 'w', name: 'W' }
    let key: string | null = null
    server.use(
      http[method](path, ({ request }) => {
        key = request.headers.get('Idempotency-Key')
        return HttpResponse.json(operation, { status: 202 })
      }),
      http.get('/api/v1/operations/op-9', () => HttpResponse.json({ ...operation, status: 'succeeded', result: model })),
    )
    await expect(call()).resolves.toEqual(model)
    expect(key).toMatch(/^[0-9a-f]{32}$/)
  })

  it('uploadModel sends an Idempotency-Key and follows a 202 to the model', async () => {
    // Stubbed below msw: jsdom's File cannot cross vitest's Request polyfill.
    printRunPoll.intervalMs = 1
    const model = { slug: 'w', name: 'W' }
    const sent: Headers[] = []
    const fetched = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      sent.push(new Headers(init?.headers))
      return String(input).endsWith('/operations/op-9')
        ? Response.json({ ...operation, status: 'succeeded', result: model })
        : Response.json(operation, { status: 202 })
    })
    try {
      await expect(api.uploadModel(new File(['cube(1);'], 'w.scad'))).resolves.toEqual(model)
    } finally {
      fetched.mockRestore()
    }
    expect(sent[0]?.get('Idempotency-Key')).toMatch(/^[0-9a-f]{32}$/)
  })

  it('deleteModel sends an Idempotency-Key and follows a 202 to its end', async () => {
    printRunPoll.intervalMs = 1
    let key: string | null = null
    server.use(
      http.delete('/api/v1/models/w', ({ request }) => {
        key = request.headers.get('Idempotency-Key')
        return HttpResponse.json(operation, { status: 202 })
      }),
      http.get('/api/v1/operations/op-9', () => HttpResponse.json({ ...operation, status: 'succeeded', result: {} })),
    )
    await expect(api.deleteModel('w')).resolves.toBeUndefined()
    expect(key).toMatch(/^[0-9a-f]{32}$/)
  })

  it('deleteModel answers a 204 at once', async () => {
    server.use(http.delete('/api/v1/models/w', () => new HttpResponse(null, { status: 204 })))
    await expect(api.deleteModel('w')).resolves.toBeUndefined()
  })
})

describe("a model's edits are commands (#1054)", () => {
  afterEach(() => {
    printRunPoll.intervalMs = 1000
  })

  const operation = { id: 'op-8', kind: 'model_source_put', subject: 'w', status: 'running', created_at: '2026-10-04T00:00:00Z' }
  const model = { slug: 'w', name: 'W' }

  it.each([
    ['replaceSource', () => api.replaceSource('w', 'cube(2);'), 'put', '/api/v1/models/w/source', model],
    ['resolveUpstreamMerge', () => api.resolveUpstreamMerge('w', 'cube(2);', 'abc1234'), 'put', '/api/v1/models/w/source', model],
    ['removeThumbnail', () => api.removeThumbnail('w'), 'delete', '/api/v1/models/w/thumbnail', model],
    ['setReadme', () => api.setReadme('w', '# W'), 'put', '/api/v1/models/w/readme', model],
    ['removeReadme', () => api.removeReadme('w'), 'delete', '/api/v1/models/w/readme', model],
    ['mergeUpstream', () => api.mergeUpstream('w'), 'post', '/api/v1/models/w/upstream/merge', { model, taken: [], kept: [] }],
    ['dismissUpstream', () => api.dismissUpstream('w'), 'post', '/api/v1/models/w/upstream/dismiss', model],
    ['detachUpstream', () => api.detachUpstream('w'), 'post', '/api/v1/models/w/upstream/detach', model],
    ['restoreVersion', () => api.restoreVersion('w', 'abc1234'), 'post', '/api/v1/models/w/versions/abc1234/restore', { id: 'abc1234', current: true }],
  ] as const)('%s sends an Idempotency-Key and follows a 202 to its result', async (_name, call, method, path, result) => {
    printRunPoll.intervalMs = 1
    let key: string | null = null
    server.use(
      http[method](path, ({ request }) => {
        key = request.headers.get('Idempotency-Key')
        return HttpResponse.json(operation, { status: 202 })
      }),
      http.get('/api/v1/operations/op-8', () => HttpResponse.json({ ...operation, status: 'succeeded', result })),
    )
    await expect(call()).resolves.toEqual(result)
    expect(key).toMatch(/^[0-9a-f]{32}$/)
  })

  it('setThumbnail sends an Idempotency-Key and follows a 202 to the model', async () => {
    // Stubbed below msw, as uploadModel's: jsdom's Blob cannot cross vitest's Request polyfill.
    printRunPoll.intervalMs = 1
    const sent: Headers[] = []
    const fetched = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      sent.push(new Headers(init?.headers))
      return String(input).endsWith('/operations/op-8')
        ? Response.json({ ...operation, status: 'succeeded', result: model })
        : Response.json(operation, { status: 202 })
    })
    try {
      await expect(api.setThumbnail('w', new Blob(['png']))).resolves.toEqual(model)
    } finally {
      fetched.mockRestore()
    }
    expect(sent[0]?.get('Idempotency-Key')).toMatch(/^[0-9a-f]{32}$/)
  })

  it('replaceSource sends the base the edit was made against', async () => {
    let sent: unknown
    server.use(
      http.put('/api/v1/models/w/source', async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json(model)
      }),
    )
    await api.replaceSource('w', 'cube(2);', false, undefined, 'abc1234')
    expect(sent).toMatchObject({ source: 'cube(2);', base: 'abc1234' })
  })

  it("keeps a merge conflict's merged text, answered by the route", async () => {
    server.use(
      http.post('/api/v1/models/w/upstream/merge', () =>
        HttpResponse.json({ title: 'Conflict', status: 409, detail: 'conflicts', merged: '<<<<<<< w', merge_base: 'abc1234' }, { status: 409 }),
      ),
    )
    const caught = await api.mergeUpstream('w').catch((e: unknown) => e)
    expect((caught as ApiError).status).toBe(409)
    expect((caught as ApiError).problem.merged).toBe('<<<<<<< w')
  })
})

describe('render (#1053)', () => {
  const defaults = { ...printRunPoll }
  afterEach(() => {
    Object.assign(printRunPoll, defaults)
  })

  it('sends a render again while the server is still accepting it', async () => {
    printRunPoll.intervalMs = 1
    let posts = 0
    server.use(
      http.post('/api/v1/models/box/render', () => {
        posts += 1
        return posts === 1
          ? HttpResponse.json(
              {
                type: 'https://scadbuddy.dev/problems/command-still-accepting',
                title: 'Service Unavailable',
                status: 503,
                detail: 'ScadBuddy is still checking this request.',
              },
              { status: 503, headers: { 'Retry-After': '2' } },
            )
          : HttpResponse.json({ job_id: 'j1', status_url: '/api/v1/jobs/j1' }, { status: 202 })
      }),
    )

    await expect(api.render('box', { params: {} })).resolves.toMatchObject({ job_id: 'j1' })
    expect(posts).toBe(2)
  })

  const accepting = () =>
    HttpResponse.json(
      {
        type: 'https://scadbuddy.dev/problems/command-still-accepting',
        title: 'Service Unavailable',
        status: 503,
        detail: 'ScadBuddy is still checking this request.',
      },
      { status: 503, headers: { 'Retry-After': '2' } },
    )

  it('re-sends with the same Idempotency-Key, so the server counts one request', async () => {
    printRunPoll.intervalMs = 1
    const keys: (string | null)[] = []
    server.use(
      http.post('/api/v1/models/box/render', ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return keys.length === 1
          ? accepting()
          : HttpResponse.json({ job_id: 'j1', status_url: '/api/v1/jobs/j1' }, { status: 202 })
      }),
    )

    await api.render('box', { params: {} })
    expect(keys).toHaveLength(2)
    expect(keys[0]).toMatch(/^[0-9a-f]{32}$/)
    expect(keys[1]).toBe(keys[0])
  })

  it('stops re-sending once its signal aborts (a superseded preview)', async () => {
    printRunPoll.intervalMs = 20
    let posts = 0
    const controller = new AbortController()
    server.use(
      http.post('/api/v1/models/box/render', () => {
        posts += 1
        controller.abort()
        return accepting()
      }),
    )

    await expect(api.render('box', { params: {} }, undefined, undefined, controller.signal)).rejects.toBeDefined()
    await new Promise((resolve) => setTimeout(resolve, 60))
    expect(posts).toBe(1)
  })
})

describe('Retry-After on problems (#1000)', () => {
  it("keeps a 429's or 503's delay in seconds, and nothing on other statuses", async () => {
    server.use(
      http.post('/api/v1/ai/credentials/test', () =>
        HttpResponse.json({ detail: 'a connection test ran moments ago' }, { status: 429, headers: { 'Retry-After': '7' } }),
      ),
      http.get('/api/v1/ai/credentials', () =>
        HttpResponse.json({ detail: 'nope' }, { status: 400, headers: { 'Retry-After': '7' } }),
      ),
    )
    await expect(api.testAiCredential()).rejects.toMatchObject({ status: 429, problem: { retry_after: 7 } })
    const other = await api.getAiCredential().catch((cause: unknown) => cause)
    expect((other as ApiError).problem).not.toHaveProperty('retry_after')
  })
})
