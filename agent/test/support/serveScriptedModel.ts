// The scripted model endpoint for `frontend/e2e/real-agent.spec.ts` (#1923), as a
// process of its own: save a gateway credential whose `base_url` is the URL this
// prints, and the agent's Claude Code talks to it instead of a model.
//
//   node test/support/serveScriptedModel.ts [port]
//
// Node runs the TypeScript as it is (type stripping), hence the `.ts` imports.
import { startFakeAnthropic } from './fakeAnthropic.ts'
import { scriptedReply } from './realAgentScript.ts'

const port = Number(process.argv[2] ?? 0)
if (!Number.isInteger(port) || port < 0 || port > 65535) {
  console.error(`usage: node test/support/serveScriptedModel.ts [port]  (port: 0-65535, 0 picks a free one; got ${process.argv[2]})`)
  process.exit(2)
}
const fake = await startFakeAnthropic((request) => scriptedReply(request.body), port)
console.log(fake.url)
process.on('SIGINT', () => void fake.close().then(() => process.exit(0)))
process.on('SIGTERM', () => void fake.close().then(() => process.exit(0)))
