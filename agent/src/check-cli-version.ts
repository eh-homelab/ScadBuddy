import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { bundledCliVersion, sdkDeclaredCliVersion } from './harness/cliVersion.js'

// Build-time assertion, run by the Dockerfile's `agent` stage:
//
//   node dist/check-cli-version.js
//
// Fails when the platform's bundled binary is missing, or its own `--version`
// differs from the Claude Code version the Agent SDK declares: a broken
// optional-dependency install could leave a different (or no) binary behind.
// The version itself is the SDK's, which agent/package.json pins exactly, so
// the build keeps no second pin of it (#1540).

const configDir = await mkdtemp(path.join(tmpdir(), 'claude-version-'))
try {
  const declared = await sdkDeclaredCliVersion()
  const actual = await bundledCliVersion(configDir)
  if (actual !== declared) {
    console.error(`ERROR: the Agent SDK declares Claude Code '${declared}', but its binary reports '${actual}'.`)
    process.exitCode = 1
  } else {
    console.log(`Claude Code ${actual} (bundled by the Agent SDK) is the version the SDK declares.`)
  }
} finally {
  await rm(configDir, { recursive: true, force: true })
}
