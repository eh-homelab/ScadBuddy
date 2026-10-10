// Tools Settings can turn off (#1911). A switched-off tool stays listed, and a
// call to it is refused in registry.ts `runToolWithOutcome`, the one entry
// point every projection takes (the harness's in-process server, /mcp, and a
// durable session's tool activities), before its handler runs. ToolServices
// `switchedOff` is the check; main.ts wires it to `ai_settings`.
//
//   get_printer_camera   `printer_camera_enabled`: a printer's camera frame is
//                        privacy-sensitive (#251). On unless stored false
//                        (routes/printerCamera.ts): it shipped on (#957).

/** `ai_settings` key for get_printer_camera; anything but a stored `false` is on. */
export const SETTING_PRINTER_CAMERA = 'printer_camera_enabled'

type Switch = { setting: string; what: string }

const SWITCHES: Readonly<Record<string, Switch>> = {
  get_printer_camera: { setting: SETTING_PRINTER_CAMERA, what: 'Printer camera' },
}

/** Every switch is on unless stored `false`. */
export function switchOn(stored: unknown): boolean {
  return stored !== false
}

/** Why `tool` may not run now, or undefined when it may. */
export type SwitchedOff = (tool: string) => Promise<string | undefined>

/**
 * The check over `ai_settings`. Without a database every tool is on, since
 * nothing can turn one off; a setting that cannot be read refuses, so a
 * database blip never turns the camera back on.
 */
export function toolSwitches(settings: { get<T>(key: string): Promise<T | undefined> } | undefined): SwitchedOff {
  return async (tool) => {
    const sw = SWITCHES[tool]
    if (!sw || !settings) return undefined
    let stored: unknown
    try {
      stored = await settings.get<unknown>(sw.setting)
    } catch {
      return `${tool} is refused: its Settings switch (${sw.what}) cannot be read`
    }
    return switchOn(stored)
      ? undefined
      : `${tool} is turned off in Settings (${sw.what}); the user can turn it back on there`
  }
}
