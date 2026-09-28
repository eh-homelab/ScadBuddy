import { createRequire } from 'node:module'
import { realpathSync } from 'node:fs'
import path from 'node:path'

// Whether the headless browser's Chromium can run WITH its sandbox here (review
// of #518, finding 3). @playwright/mcp leaves `chromiumSandbox` false on Linux
// when no channel is set, so Chromium runs with `--no-sandbox`; the harness now
// asks for the sandbox whenever this probe says it works.
//
// Measured in the `agent` image (uid 10001, read-only root, tmpfs /tmp and state
// dir), `chromium_headless_shell-1246`:
//   - Docker's default seccomp profile: the sandbox fails ("Chromium sandboxing
//     failed!"): it needs unprivileged user namespaces, which that profile's
//     `clone`/`unshare` rules deny to a process without CAP_SYS_ADMIN;
//   - `--security-opt seccomp=unconfined`: it starts, and the renderer runs
//     without `--no-sandbox`.
// So a pod gets the sandbox when its seccomp profile allows user namespaces
// (e.g. `Unconfined`, or a Localhost profile that allows `clone`/`unshare` with
// CLONE_NEWUSER) and the node allows them (`user.max_user_namespaces` > 0), and
// not under `RuntimeDefault`. docs/ai/headless-browser.md, "Sandbox", has the
// operator's side.
//
// The probe launches the pinned Chromium once with the sandbox on and loads a
// blank page. It never throws: any failure means "no sandbox", with the reason.

export type SandboxProbe = { available: boolean; detail: string }

type Launcher = {
  launch(options: { headless: boolean; chromiumSandbox: boolean; timeout: number; executablePath?: string }): Promise<{
    newPage(): Promise<{ setContent(html: string): Promise<void> }>
    close(): Promise<void>
  }>
}

/** playwright-core as @playwright/mcp resolves it (its own dependency, not agent/'s). */
function chromiumOfPlaywrightMcp(): Launcher {
  const require = createRequire(import.meta.url)
  const mcpDir = path.dirname(realpathSync(require.resolve('@playwright/mcp/package.json')))
  const fromMcp = createRequire(path.join(mcpDir, 'package.json'))
  return (fromMcp('playwright-core') as { chromium: Launcher }).chromium
}

export async function probeChromiumSandbox(
  options: { executablePath?: string; timeoutMs?: number; launcher?: Launcher } = {},
): Promise<SandboxProbe> {
  try {
    const launcher = options.launcher ?? chromiumOfPlaywrightMcp()
    const browser = await launcher.launch({
      headless: true,
      chromiumSandbox: true,
      timeout: options.timeoutMs ?? 20_000,
      ...(options.executablePath ? { executablePath: options.executablePath } : {}),
    })
    try {
      const page = await browser.newPage()
      await page.setContent('<p>sandbox probe</p>')
    } finally {
      await browser.close()
    }
    return { available: true, detail: 'Chromium started with its sandbox' }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    const line = message.split('\n').find((l) => /sandbox/i.test(l)) ?? message.split('\n')[0] ?? message
    return { available: false, detail: line.trim().slice(0, 300) }
  }
}
