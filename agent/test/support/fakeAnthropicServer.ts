// The fake Anthropic endpoint as a process, for tests outside Node (agent-durable's
// pytest): `node test/support/fakeAnthropicServer.ts <script.json>`. The script is a
// JSON list of `Reply`; each /v1/messages call that offers tools gets the next one, and
// a call past the end gets a 500. The first stdout line is `{"url": …}`; `GET /__requests` returns the
// recorded requests. Node 24 runs this file with type stripping, so it (like
// fakeAnthropic.ts) uses only erasable syntax, and imports the `.ts` file at run time.
import { readFileSync } from 'node:fs'
import type { Reply, startFakeAnthropic as Start } from './fakeAnthropic.js'

const scriptPath = process.argv[2]
if (!scriptPath) {
  process.stderr.write('usage: fakeAnthropicServer.ts <script.json>\n')
  process.exit(2)
}
const script = JSON.parse(readFileSync(scriptPath, 'utf8')) as Reply[]
const { startFakeAnthropic } = (await import(new URL('./fakeAnthropic.ts', import.meta.url).href)) as {
  startFakeAnthropic: typeof Start
}

// Claude Code's side queries (the session title) offer no tools; they get this reply
// and take no script entry, so the script is the agent loop's turns only.
const SIDE: Reply = { text: 'Side reply' }
const EXHAUSTED: Reply = { error: { status: 500, type: 'api_error', message: 'the fake endpoint script is exhausted' } }

let next = 0
const fake = await startFakeAnthropic((r) => (r.body?.tools?.length ? (script[next++] ?? EXHAUSTED) : SIDE))
process.stdout.write(`${JSON.stringify({ url: fake.url })}\n`)

const stop = () => void fake.close().then(() => process.exit(0))
process.on('SIGTERM', stop)
process.on('SIGINT', stop)
