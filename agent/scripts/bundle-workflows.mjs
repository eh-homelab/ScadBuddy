// The workflow bundles (#1055), built once at `pnpm build` as the Temporal TypeScript SDK
// recommends for production (`bundleWorkflowCode`, then
// `Worker.create({ workflowBundle: { codePath } })`), so the service never runs webpack at
// start: `workflow-bundle.js`, every workflow, for the shared queues' workers; and
// `bridge-bundle.js`, BridgeCall alone, for each replica's host worker (#5862,
// src/bridge/relay.ts).
import { bundleWorkflowCode } from '@temporalio/worker'
import { writeFile } from 'node:fs/promises'
import { URL, fileURLToPath } from 'node:url'

// Failures' messages sealed with the payloads (src/temporal/failureConverter.ts).
const failureConverterPath = fileURLToPath(new URL('../dist/temporal/failureConverter.js', import.meta.url))
const bundles = { 'workflow-bundle.js': 'workflows.js', 'bridge-bundle.js': 'bridgeCall.js' }
for (const [bundle, source] of Object.entries(bundles)) {
  const { code } = await bundleWorkflowCode({
    workflowsPath: fileURLToPath(new URL(`../dist/temporal/${source}`, import.meta.url)),
    failureConverterPath,
  })
  await writeFile(new URL(`../dist/temporal/${bundle}`, import.meta.url), code)
}
