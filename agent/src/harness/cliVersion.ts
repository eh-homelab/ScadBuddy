import { execFile } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

// The Agent SDK "runs the Claude Code binary"
// (https://code.claude.com/docs/en/agent-sdk/overview), shipped as a per-platform
// optional dependency. The image build asserts that binary is the version the
// SDK declares (#261, spec §4.4; check-cli-version.ts, #1540), and these
// helpers are how the build and the tests read both.
//
// Measured on @anthropic-ai/claude-agent-sdk 0.3.283, 2026-09-27, and again on
// 0.3.287, 2026-10-06 (the values below are 0.3.287's):
//   - the SDK's package.json declares `"claudeCodeVersion": "2.1.287"`;
//   - the SDK resolves the binary as
//     `@anthropic-ai/claude-agent-sdk-<platform>-<arch>[-musl]/claude`
//     relative to itself (read from its bundled sdk.mjs);
//   - `claude --version` prints `2.1.287 (Claude Code)` on STDOUT and exits 0.

const execFileAsync = promisify(execFile)

function sdkRequire(): NodeJS.Require {
  return createRequire(fileURLToPath(import.meta.resolve('@anthropic-ai/claude-agent-sdk')))
}

/** The Claude Code version the installed SDK declares it bundles. */
export async function sdkDeclaredCliVersion(): Promise<string> {
  const sdkEntry = fileURLToPath(import.meta.resolve('@anthropic-ai/claude-agent-sdk'))
  const manifest = JSON.parse(
    await readFile(path.join(path.dirname(sdkEntry), 'package.json'), 'utf8'),
  ) as { claudeCodeVersion?: unknown }
  if (typeof manifest.claudeCodeVersion !== 'string') {
    throw new Error('@anthropic-ai/claude-agent-sdk package.json has no claudeCodeVersion')
  }
  return manifest.claudeCodeVersion
}

/** Absolute path of the bundled binary for this platform, the same candidates the SDK tries. */
export function bundledCliPath(): string {
  const base = `@anthropic-ai/claude-agent-sdk-${process.platform}-${process.arch}`
  const require = sdkRequire()
  for (const candidate of [`${base}/claude`, `${base}-musl/claude`]) {
    try {
      return require.resolve(candidate)
    } catch {
      // try the next candidate
    }
  }
  throw new Error(`no bundled Claude Code binary for ${process.platform}-${process.arch}`)
}

/** Parses `2.1.283 (Claude Code)` → `2.1.283`. */
export function parseCliVersion(stdout: string): string {
  const match = /^(\d+\.\d+\.\d+\S*) \(Claude Code\)$/m.exec(stdout.trim())
  if (!match?.[1]) throw new Error(`unrecognised claude --version output: ${JSON.stringify(stdout)}`)
  return match[1]
}

/** Runs the bundled binary's `--version`. It touches no network and no credentials. */
export async function bundledCliVersion(configDir: string): Promise<string> {
  const { stdout } = await execFileAsync(bundledCliPath(), ['--version'], {
    // Isolated from any host ~/.claude, like every query (options.ts).
    env: { CLAUDE_CONFIG_DIR: configDir, HOME: configDir, PATH: process.env.PATH ?? '' },
    timeout: 30_000,
  })
  return parseCliVersion(stdout)
}
