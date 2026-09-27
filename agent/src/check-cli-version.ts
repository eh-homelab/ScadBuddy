import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { bundledCliVersion, sdkDeclaredCliVersion } from './harness/cliVersion.js'

// Build-time assertion, run by the Dockerfile's `agent` stage:
//
//   node dist/check-cli-version.js "$CLAUDE_CODE_VERSION"
//
// Fails when either the SDK's declared Claude Code version or the bundled
// binary's own `--version` differs from the pin. Both are checked because an
// SDK bump moves the declaration and a broken optional-dependency install
// could leave a different (or no) binary behind.

const expected = process.argv[2]
if (!expected) {
  console.error('usage: check-cli-version <expected Claude Code version>')
  process.exit(2)
}

const configDir = await mkdtemp(path.join(tmpdir(), 'claude-version-'))
try {
  const declared = await sdkDeclaredCliVersion()
  const actual = await bundledCliVersion(configDir)
  if (declared !== expected || actual !== expected) {
    console.error(
      `ERROR: the Agent SDK declares Claude Code '${declared}' and its binary reports '${actual}'; ` +
        `this build pins '${expected}'.`,
    )
    console.error('       Re-verify the harness facts in the AI design spec, then bump the pin.')
    process.exitCode = 1
  } else {
    console.log(`Claude Code ${actual} (bundled by the Agent SDK) matches the pin.`)
  }
} finally {
  await rm(configDir, { recursive: true, force: true })
}
