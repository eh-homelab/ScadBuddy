// The AgentOperation workflow bundle (#1055), built once at `pnpm build` as the
// Temporal TypeScript SDK recommends for production (`bundleWorkflowCode`, then
// `Worker.create({ workflowBundle: { codePath } })`), so the service never runs
// webpack at start.
import { bundleWorkflowCode } from '@temporalio/worker'
import { writeFile } from 'node:fs/promises'
import { URL, fileURLToPath } from 'node:url'

const { code } = await bundleWorkflowCode({
  workflowsPath: fileURLToPath(new URL('../dist/temporal/workflows.js', import.meta.url)),
})
await writeFile(new URL('../dist/temporal/workflow-bundle.js', import.meta.url), code)
