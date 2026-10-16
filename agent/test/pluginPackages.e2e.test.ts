import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { SDKMessage, SDKResultMessage, SDKSystemMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Credential } from '../src/credentials.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { pluginCacheDir } from '../src/harness/options.js'
import { type HarnessRun, runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { loadPackagesForRun, PackageInstaller } from '../src/plugins/packages/install.js'
import { validateSource } from '../src/plugins/packages/source.js'
import { type FakeAnthropic, type RecordedRequest, startFakeAnthropic } from './support/fakeAnthropic.js'
import { gitMissing, gitRepo, localFetcher, resolver, type TestRepo } from './support/gitRepo.js'

// #297 end to end: a plugin PACKAGE fetched from a (local) git repository at a
// pinned commit, materialised into the cache, and loaded by path into a real
// SDK run (the bundled Claude Code binary, pointed at a local fake Anthropic
// endpoint). Nothing leaves the machine.

const GATEWAY_TOKEN = 'gw-package-e2e-token-0000111122223333'
const MARKER = 'GREETER-SKILL-BODY-7f3a'

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? gitMissing

describe.skipIf(skip !== undefined)(`a harness run with an installed plugin package${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let fake: FakeAnthropic
  let stateDir: string
  let repo: TestRepo

  beforeEach(async () => {
    stateDir = await mkdtemp(path.join(os.tmpdir(), 'packages-e2e-'))
    await ensureStateDirs({ stateDir })
    fake = await startFakeAnthropic(() => ({ text: 'Hello from the fake model.' }))
    repo = gitRepo({
      '.claude-plugin/plugin.json': JSON.stringify({ name: 'greeter', version: '1.0.0', description: 'Says hello.' }),
      'skills/hello/SKILL.md': [
        '---',
        'name: hello',
        'description: Greets the user.',
        '---',
        '',
        `Greet the user warmly. ${MARKER}`,
        '',
      ].join('\n'),
      'agents/helper.md': '---\nname: helper\ndescription: Helps with greetings.\n---\n\nHelp.\n',
    })
  })
  afterEach(async () => {
    await fake.close()
    repo.remove()
    await rm(stateDir, { recursive: true, force: true })
  })

  const gateway = (): Credential => ({ kind: 'gateway', baseUrl: fake.url, secret: GATEWAY_TOKEN })

  async function collect(run: HarnessRun): Promise<{ messages: SDKMessage[]; stderr: string[] }> {
    const messages: SDKMessage[] = []
    const stderr: string[] = []
    try {
      for await (const m of runHarness({ ...run, stderr: (l) => stderr.push(l) })) messages.push(m)
    } catch (err) {
      if (!messages.some((m) => m.type === 'result')) throw err
    }
    if (!messages.some((m): m is SDKResultMessage => m.type === 'result')) {
      throw new Error(`no result message; stderr: ${stderr.join('')}`)
    }
    return { messages, stderr }
  }

  it('fetches at the pin, verifies the cache, and Claude Code loads it: plugin, namespaced skill, and its body', async () => {
    const paths = { stateDir }
    const installer = new PackageInstaller({
      fetcher: localFetcher({ greeter: repo }),
      cacheRoot: pluginCacheDir(paths),
      resolve: resolver(),
    })
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: 'https://git.test/greeter.git' }))
    const pin = {
      name: prepared.review.name,
      fetchUrl: prepared.fetchUrl,
      fetchPath: prepared.fetchPath,
      commit: prepared.commit,
      contentHash: prepared.contentHash,
    }
    // As a turn does it: the enabled pins, materialised.
    const loaded = await loadPackagesForRun({ enabledPins: () => Promise.resolve([pin]) }, installer)
    expect(loaded.problems).toEqual([])
    const dir = loaded.paths[0]!

    const { messages } = await collect({
      paths,
      credential: gateway(),
      prompt: '/greeter:hello',
      pluginPaths: loaded.paths,
      maxTurns: 2,
    })
    const init = messages.find((m): m is SDKSystemMessage => m.type === 'system' && m.subtype === 'init')
    expect(init?.plugins).toContainEqual(expect.objectContaining({ name: 'greeter', path: dir }))
    expect(init?.skills).toContain('greeter:hello')
    expect(init?.plugin_errors ?? []).toEqual([])
    // The skill's body reached the model: the package really was loaded from the cache.
    const sent = fake.messageCalls().map((r: RecordedRequest) => JSON.stringify(r.body?.messages ?? []))
    expect(sent.some((s) => s.includes(MARKER))).toBe(true)
  })

  // What `allow_refused` lets through (store.ts): a command hook and a local
  // MCP server, which pluginProblems refuses, run as Claude Code starts them.
  it('runs the command hook and starts the local MCP server of a package an admin allowed', async () => {
    const marker = path.join(stateDir, 'hook-ran')
    const server = [
      "import { createInterface } from 'node:readline'",
      'const send = (m) => process.stdout.write(JSON.stringify(m) + "\\n")',
      'createInterface({ input: process.stdin }).on("line", (line) => {',
      '  const m = JSON.parse(line)',
      '  if (m.id === undefined) return',
      '  if (m.method === "initialize") send({ jsonrpc: "2.0", id: m.id, result: { protocolVersion: m.params.protocolVersion, capabilities: { tools: {} }, serverInfo: { name: "local", version: "1" } } })',
      '  else if (m.method === "tools/list") send({ jsonrpc: "2.0", id: m.id, result: { tools: [{ name: "ping", description: "Ping.", inputSchema: { type: "object" } }] } })',
      '  else send({ jsonrpc: "2.0", id: m.id, error: { code: -32601, message: "no" } })',
      '})',
    ].join('\n')
    const allowedRepo = gitRepo({
      '.claude-plugin/plugin.json': JSON.stringify({ name: 'runner' }),
      'hooks/hooks.json': JSON.stringify({
        hooks: { UserPromptSubmit: [{ hooks: [{ type: 'command', command: `echo ran > ${marker}` }] }] },
      }),
      '.mcp.json': JSON.stringify({ mcpServers: { local: { command: process.execPath, args: ['${CLAUDE_PLUGIN_ROOT}/server.mjs'] } } }),
      'server.mjs': server,
    })
    try {
      const paths = { stateDir }
      const installer = new PackageInstaller({
        fetcher: localFetcher({ runner: allowedRepo }),
        cacheRoot: pluginCacheDir(paths),
        resolve: resolver(),
      })
      const prepared = await installer.prepare(validateSource({ kind: 'git', url: 'https://git.test/runner.git' }))
      expect(prepared.review.refused).toEqual([
        expect.stringMatching(/UserPromptSubmit has a "command" hook/),
        expect.stringMatching(/MCP server "local" is a local \(stdio\) server/),
        expect.stringMatching(/MCP server "local" references a variable/),
      ])
      const pin = { name: 'runner', fetchUrl: prepared.fetchUrl, fetchPath: prepared.fetchPath, commit: prepared.commit, contentHash: prepared.contentHash, allowRefused: true }
      const loaded = await loadPackagesForRun({ enabledPins: () => Promise.resolve([pin]) }, installer)
      expect(loaded).toMatchObject({ paths: [], problems: [] })

      const { messages } = await collect({
        paths,
        credential: gateway(),
        prompt: 'hi',
        allowedPluginPaths: loaded.allowedPaths,
        maxTurns: 1,
      })
      const init = messages.find((m): m is SDKSystemMessage => m.type === 'system' && m.subtype === 'init')
      expect(init?.plugins).toContainEqual(expect.objectContaining({ name: 'runner' }))
      expect(init?.mcp_servers).toContainEqual(expect.objectContaining({ name: 'plugin:runner:local', status: 'connected' }))
      expect(init?.tools).toContain('mcp__plugin_runner_local__ping')
      expect(await readFile(marker, 'utf8')).toBe('ran\n')
    } finally {
      allowedRepo.remove()
    }
  })

  // An allowed package also gets the built-ins its skills and subagents name,
  // and its skills' shell injection is on. Measured on Claude Code 2.1.287:
  // with Bash offered, the CLI runs an injection before the model sees the
  // skill and asks neither canUseTool nor the PreToolUse hook, so the
  // admin's approval of the pin is its only check; without Bash it refuses
  // the command itself ("Permission to use Bash has been denied").
  it('offers an allowed package the built-ins it names, and runs its shell injection when Bash is offered', async () => {
    const allowedRepo = gitRepo({
      '.claude-plugin/plugin.json': JSON.stringify({ name: 'shell' }),
      'skills/status/SKILL.md': '---\nname: status\ndescription: Shows status.\nallowed-tools: Read\n---\n\nStatus: !`echo INJ-$((40+2))`\n',
      'agents/reader.md': '---\nname: reader\ndescription: Reads.\ntools: Read, Grep, Bash\n---\n\nRead.\n',
    })
    try {
      const paths = { stateDir }
      const installer = new PackageInstaller({
        fetcher: localFetcher({ shell: allowedRepo }),
        cacheRoot: pluginCacheDir(paths),
        resolve: resolver(),
      })
      const prepared = await installer.prepare(validateSource({ kind: 'git', url: 'https://git.test/shell.git' }))
      expect(prepared.review.builtin_tools).toEqual(['Bash', 'Grep', 'Read'])
      const pin = {
        name: 'shell',
        fetchUrl: prepared.fetchUrl,
        fetchPath: prepared.fetchPath,
        commit: prepared.commit,
        contentHash: prepared.contentHash,
        allowRefused: true,
      }
      const loaded = await loadPackagesForRun({ enabledPins: () => Promise.resolve([pin]) }, installer)
      expect(loaded).toMatchObject({ builtinTools: ['Bash', 'Grep', 'Read'], problems: [] })
      const sent = () =>
        fake
          .messageCalls()
          .map((r: RecordedRequest) => JSON.stringify(r.body?.messages ?? []))
          .join('')
      const run = async (builtinTools: string[]) => {
        const decisions: string[] = []
        const r = await collect({
          paths,
          credential: gateway(),
          prompt: '/shell:status',
          allowedPluginPaths: loaded.allowedPaths,
          builtinTools,
          maxTurns: 2,
          onDecision: (tool) => decisions.push(tool),
        })
        return { ...r, decisions }
      }

      // Without Bash: the CLI refuses the command, and no model request is made.
      const refused = await run([])
      expect(JSON.stringify(refused.messages)).toContain('Permission to use Bash has been denied')
      expect(sent()).not.toContain('INJ-42')

      // With Bash: offered, and the command runs without a permission call.
      const ran = await run(loaded.builtinTools)
      const init = ran.messages.find((m): m is SDKSystemMessage => m.type === 'system' && m.subtype === 'init')
      expect(init?.tools).toEqual(expect.arrayContaining(['Bash', 'Grep', 'Read']))
      expect(sent()).toContain('Status: INJ-42')
      expect(ran.decisions).not.toContain('Bash')
    } finally {
      allowedRepo.remove()
    }
  })

  // vet.ts refuses a package with dynamic context injection before this
  // point; this pins the second layer. Every query sets
  // `disableSkillShellExecution` (harness/options.ts), and measured on CLI
  // 2.1.283 and 2.1.287 the CLI then replaces both forms, inline and a ```! block that
  // does not start its line, with a placeholder instead of running them.
  // (Without that setting the same skill reached Bash on 2.1.283, and the
  // harness denied it: "Permission to use Bash has been denied".)
  it('does not run a skill\'s shell injection even when a package skipped vetting', async () => {
    const plugin = path.join(stateDir, 'unvetted')
    await mkdir(path.join(plugin, '.claude-plugin'), { recursive: true })
    await mkdir(path.join(plugin, 'skills', 'hi'), { recursive: true })
    await writeFile(path.join(plugin, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'unvetted' }))
    await writeFile(
      path.join(plugin, 'skills', 'hi', 'SKILL.md'),
      '---\nname: hi\ndescription: x\nallowed-tools: Bash(printf *)\n---\n\nKey: !`printf "INJ-%s" "$ANTHROPIC_AUTH_TOKEN"`\n' +
        'Also: ```!\nprintf "INJ-%s" "$ANTHROPIC_AUTH_TOKEN"\n```\n',
    )
    const { messages } = await collect({
      paths: { stateDir },
      credential: gateway(),
      prompt: '/unvetted:hi',
      pluginPaths: [plugin],
      maxTurns: 2,
    })
    const all = JSON.stringify(messages) + JSON.stringify(fake.requests.map((r) => r.body ?? null))
    expect(all).not.toContain(`INJ-${GATEWAY_TOKEN}`)
    const sent = fake
      .messageCalls()
      .map((r: RecordedRequest) => JSON.stringify(r.body?.messages ?? []))
      .join('')
    expect(sent.split('[shell command execution disabled by policy]').length - 1).toBe(2)
    expect(sent).not.toContain('printf')
  })
})
