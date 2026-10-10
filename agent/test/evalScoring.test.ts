import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { describe, expect, it } from 'vitest'
import { EVAL_BACKEND_URL, EvalBackend, keychain3mf } from '../evals/backend.js'
import { claimsOf, REPO_ROOT, repoSources, scoreCitations, suggestionsOf } from '../evals/citations.js'
import { RealBackend } from '../evals/realBackend.js'
import { collectRenders, keychainProblems, KEYCHAIN_HEIGHTS } from '../evals/render.js'
import { createBackendClient } from '../src/api/backend.js'
import { readBambu3mf, readZip, writeZip } from '../evals/threemf.js'

// The two scorers #1924 adds to the evals (docs/ai/evals.md, "Citation
// scoring" and "Real-render checks"), tested on their own. test/evals.test.ts
// runs them inside the scripted scenarios.

const SKILL = 'plugins/scadbuddy/skills/authoring/SKILL.md'

describe('citation scoring: what counts as a suggestion and a citation', () => {
  it('takes each list item as one suggestion, continuation lines included', () => {
    const text = [
      'Here is what I would change:',
      '',
      '- Raise `ring_wall` to 1.8 mm,',
      '  four lines on a 0.4 mm nozzle.',
      '* Chamfer the hole.',
      '1. Keep the overhangs at 45°.',
      '2) Leave the text size alone.',
      '',
      'That is all.',
    ].join('\n')
    expect(suggestionsOf(text).map((s) => s.text)).toEqual([
      'Raise `ring_wall` to 1.8 mm, four lines on a 0.4 mm nozzle.',
      'Chamfer the hole.',
      'Keep the overhangs at 45°.',
      'Leave the text size alone.',
    ])
    expect(suggestionsOf('No list here, just prose.')).toEqual([])
  })

  it('finds URLs, and repository paths with their § or section, as lint-plugin.sh accepts them', () => {
    const [s] = suggestionsOf(
      `- Chamfer it ([Bambu Lab Wiki](https://wiki.bambulab.com/en/software/x).) and see \`${SKILL}\` §9, ` +
        'and `docs/superpowers/specs/2026-09-22-scadbuddy-design.md` section "Verified facts the design rests on"; ' +
        'also models/name-keychain/model.scad.',
    )
    expect(s?.citations).toEqual([
      { kind: 'url', url: 'https://wiki.bambulab.com/en/software/x', host: 'wiki.bambulab.com' },
      { kind: 'path', path: SKILL, section: '9' },
      {
        kind: 'path',
        path: 'docs/superpowers/specs/2026-09-22-scadbuddy-design.md',
        section: 'Verified facts the design rests on',
      },
      { kind: 'path', path: 'models/name-keychain/model.scad' },
    ])
  })

  it('takes a section written before its path when none follows it', () => {
    const [s] = suggestionsOf(`- Per §6.2 of docs/superpowers/specs/2026-09-22-scadbuddy-design.md, keep it.`)
    expect(s?.citations).toEqual([{ kind: 'path', path: 'docs/superpowers/specs/2026-09-22-scadbuddy-design.md', section: '6.2' }])
  })

  it('a claim is a number with a unit, outside the citations', () => {
    expect(claimsOf(`Raise it to 1.8 mm (0.4mm nozzle), keep 45° and 20 %, see ${SKILL} §9 and https://x.example/3mm`)).toEqual([
      '1.8',
      '0.4',
      '45',
      '20',
    ])
    expect(claimsOf('Make the hole bigger.')).toEqual([])
  })
})

describe('citation scoring: resolving sources in the repository', () => {
  const sources = repoSources()

  it('reads a Markdown section by number, up to the next heading of its level', () => {
    const found = sources.resolve({ kind: 'path', path: SKILL, section: '9' })
    expect(found.ok).toBe(true)
    if (found.ok) {
      expect(found.text).toContain('### Holes and fits')
      expect(found.text).not.toContain('## 10. OpenSCAD pitfalls')
    }
    // 6.2 is not 6.2.1, and a subsection ends at the next heading of its level.
    const spec = sources.resolve({ kind: 'path', path: 'docs/superpowers/specs/2026-09-22-scadbuddy-design.md', section: '6.2' })
    expect(spec.ok && spec.text.split('\n')[0]).toBe('### 6.2 Bambu-style 3MF writer')
  })

  it('reads a Markdown section by name', () => {
    const found = sources.resolve({ kind: 'path', path: SKILL, section: 'Holes and fits' })
    expect(found.ok && found.text).toContain('Printed holes come out undersized')
  })

  it('names what does not resolve', () => {
    expect(sources.resolve({ kind: 'path', path: 'docs/no-such-file.md', section: '1' })).toEqual({
      ok: false,
      reason: 'docs/no-such-file.md does not exist',
    })
    expect(sources.resolve({ kind: 'path', path: SKILL, section: '42' })).toEqual({
      ok: false,
      reason: `${SKILL} has no section 42`,
    })
    expect(sources.resolve({ kind: 'path', path: SKILL })).toEqual({
      ok: false,
      reason: `${SKILL} is Markdown: cite its § or section`,
    })
    expect(sources.resolve({ kind: 'path', path: '../outside.md', section: '1' })).toEqual({
      ok: false,
      reason: '../outside.md is outside the repository',
    })
  })

  it('takes a whole non-Markdown file, which has no sections to cite', () => {
    const found = sources.resolve({ kind: 'path', path: 'models/name-keychain/model.scad' })
    expect(found.ok && found.text).toContain('ring_wall')
  })

  it('knows the hosts the plugin skills cite', () => {
    expect(sources.knownHosts.has('wiki.bambulab.com')).toBe(true)
    expect(sources.knownHosts.has('example.com')).toBe(false)
    expect(REPO_ROOT).toMatch(/[/\\]$/)
  })
})

describe('citation scoring: each suggestion against its sources', () => {
  const sources = repoSources()

  it('supports a claim whose numbers the cited section states', () => {
    const [score] = scoreCitations(`- Give sliding fits 0.3 mm of clearance (\`${SKILL}\` §9).`, sources)
    expect(score).toMatchObject({ cited: true, unresolved: [], unsupported: [], unchecked: [] })
  })

  it('a number the cited source does not state is unsupported', () => {
    const [score] = scoreCitations(`- Give sliding fits 0.7 mm of clearance (\`${SKILL}\` §9).`, sources)
    expect(score?.unsupported).toEqual(['0.7'])
  })

  it('a number may come from any source the suggestion cites', () => {
    const [score] = scoreCitations(
      `- Raise ring_wall from 1.6 mm to 1.8 mm, four lines on a 0.4 mm nozzle (\`${SKILL}\` §9; models/name-keychain/model.scad).`,
      sources,
    )
    expect(score?.unsupported).toEqual([])
  })

  it('an uncited suggestion, an unknown host and a missing section are each named', () => {
    const scores = scoreCitations(
      [
        '- Make the hole 5 mm.',
        '- Use 0.3 mm ([blog](https://example.com/fits)).',
        `- Use 0.3 mm (${SKILL} §77).`,
      ].join('\n'),
      sources,
    )
    expect(scores.map((s) => [s.cited, s.unresolved])).toEqual([
      [false, []],
      [true, ['https://example.com/fits: example.com is not a host the plugin skills cite']],
      [true, [`${SKILL} has no section 77`]],
    ])
  })

  it('a claim cited only to a known URL is unchecked, not failed', () => {
    const [score] = scoreCitations(
      '- Hole compensation: 0.24 mm ([Bambu](https://wiki.bambulab.com/en/software/bambu-studio/xy-hole-contour-compensation)).',
      sources,
    )
    expect(score).toMatchObject({ cited: true, unresolved: [], unsupported: [], unchecked: ['0.24'] })
  })
})

describe('real-render checks: a backend over HTTP', () => {
  it('records what the tools send, forwards it unchanged, and follows the render to its 3MF', async () => {
    // A stand-in for the image: the recorded backend's answers, served over HTTP.
    const behind = new EvalBackend()
    const seen: string[] = []
    const server = createServer((req, res) => {
      void (async () => {
        const chunks: Buffer[] = []
        for await (const chunk of req) chunks.push(chunk as Buffer)
        seen.push(`${req.method} ${req.url} ${req.headers['idempotency-key'] ?? ''}`)
        const body = chunks.length ? Buffer.concat(chunks) : undefined
        const answer = await behind.peek(req.url ?? '/', { method: req.method ?? 'GET', ...(body ? { body } : {}) })
        res.writeHead(answer.status, Object.fromEntries(answer.headers))
        res.end(Buffer.from(await answer.arrayBuffer()))
      })()
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    try {
      const { port } = server.address() as AddressInfo
      const real = new RealBackend(`http://127.0.0.1:${port}/`)
      const client = createBackendClient(EVAL_BACKEND_URL, real.fetch)
      const accepted = await client.POST('/api/v1/models/{slug}/render', {
        params: { path: { slug: 'name-keychain' } },
        headers: { 'Idempotency-Key': 'k1' },
        body: { params: { name: 'Ada', text_color: 'red' } },
      })
      const jobId = accepted.data?.job_id ?? ''
      await client.POST('/api/v1/models/{slug}/outputs', { params: { path: { slug: 'name-keychain' } }, body: { job_id: jobId } })
      expect(real.log.map((r) => [r.method, r.path, r.status])).toEqual([
        ['POST', '/api/v1/models/name-keychain/render', 202],
        ['POST', '/api/v1/models/name-keychain/outputs', 201],
      ])
      expect(real.log[0]?.body).toEqual({ params: { name: 'Ada', text_color: 'red' } })
      expect(real.log[0]?.response).toMatchObject({ job_id: jobId })
      expect(seen[0]).toBe('POST /api/v1/models/name-keychain/render k1')

      const [render] = await collectRenders(real, { pollMs: 1 })
      expect(render).toMatchObject({ jobId, status: 'done', slug: 'name-keychain' })
      expect(render?.model && keychainProblems(render.model, { ...KEYCHAIN_HEIGHTS, lettersRed: true })).toEqual([])
      // The scorer's requests reached the backend, but not the log.
      expect(real.log).toHaveLength(2)
      expect(seen.some((s) => s.includes('/model.3mf'))).toBe(true)
    } finally {
      await new Promise((resolve) => server.close(resolve))
    }
  })

  it('logs a built-in slug decoded, as the checks match it', async () => {
    const real = new RealBackend('http://127.0.0.1:1')
    await real.fetch('http://backend.eval/api/v1/models/builtin%3Aname-keychain/render', { method: 'POST', body: '{}' }).catch(() => undefined)
    expect(real.log[0]?.path).toBe('/api/v1/models/builtin:name-keychain/render')
  })
})

describe('real-render checks: the 3MF', () => {
  it('reads back a zip it wrote, stored and deflated', () => {
    const zip = writeZip({ 'a.txt': 'hello', 'dir/b.bin': Buffer.alloc(2000, 7) })
    const entries = readZip(zip)
    expect(entries.get('a.txt')?.toString()).toBe('hello')
    expect(entries.get('dir/b.bin')).toEqual(Buffer.alloc(2000, 7))
  })

  it('reads a Bambu-style 3MF: each part with its extruder, colour and height', () => {
    const model = readBambu3mf(keychain3mf({ baseColour: '#1E1E1E', textColour: '#FF0000', ...KEYCHAIN_HEIGHTS }))
    expect(model.parts.map((p) => [p.extruder, p.colour, p.zMin, p.zMax])).toEqual([
      [1, '#1E1E1E', 0, 4],
      [2, '#FF0000', 4, 6.8],
    ])
    expect(model.parts.every((p) => p.triangles > 0)).toBe(true)
  })

  it('passes the keychain the parameters describe', () => {
    const model = readBambu3mf(keychain3mf({ baseColour: '#1E1E1E', textColour: 'red', ...KEYCHAIN_HEIGHTS }))
    expect(keychainProblems(model, { ...KEYCHAIN_HEIGHTS, lettersRed: true })).toEqual([])
  })

  it('names a keychain that is the wrong height, the wrong colour or one part short', () => {
    const tall = readBambu3mf(keychain3mf({ baseColour: '#1E1E1E', textColour: '#FF0000', base: 4, letters: 5 }))
    expect(keychainProblems(tall, { ...KEYCHAIN_HEIGHTS, lettersRed: true })).toEqual([
      'the letters are 5.000 mm proud, expected 2.8',
      'the model is 9.000 mm tall, expected 6.8',
    ])
    const blue = readBambu3mf(keychain3mf({ baseColour: '#1E1E1E', textColour: '#0000FF', ...KEYCHAIN_HEIGHTS }))
    expect(keychainProblems(blue, { ...KEYCHAIN_HEIGHTS, lettersRed: true })).toEqual(['the letters are #0000FF, not red'])
    const one = readBambu3mf(keychain3mf({ baseColour: '#1E1E1E', textColour: '#FF0000', ...KEYCHAIN_HEIGHTS, lettersMissing: true }))
    expect(keychainProblems(one, { ...KEYCHAIN_HEIGHTS, lettersRed: true })).toEqual(['1 non-empty part(s), expected 2 (base and letters)'])
  })
})
