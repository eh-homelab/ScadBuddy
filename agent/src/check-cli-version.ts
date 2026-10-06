import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { bundledCliVersion, sdkDeclaredCliVersion } from './harness/cliVersion.js'

// Build-time assertion, run by the Dockerfile's `agent` stage:
//
//   node dist/check-cli-version.js
//
// Fails when there is no bundled binary for this platform, or its own
// `--version` differs from the `claudeCodeVersion` the installed SDK declares:
// an optional-dependency install that left a different (or no) binary behind.
// The version itself is pinned by agent/package.json (the SDK, exactly) and the
// lockfile (each platform's binary, by integrity hash), so it is not repeated
// here as an argument (#1540).

const configDir = await mkdtemp(path.join(tmpdir(), 'claude-version-'))
try {
  const declared = await sdkDeclaredCliVersion()
  const actual = await bundledCliVersion(configDir)
  if (declared !== actual) {
    console.error(`ERROR: the Agent SDK declares Claude Code '${declared}' but its binary reports '${actual}'.`)
    console.error('       Reinstall agent/node_modules from the lockfile for this platform.')
    process.exitCode = 1
  } else {
    console.log(`Claude Code ${actual} (bundled by the Agent SDK) is the version the SDK declares.`)
  }
} finally {
  await rm(configDir, { recursive: true, force: true })
}
