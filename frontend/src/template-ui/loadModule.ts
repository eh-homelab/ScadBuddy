export type UiModuleLoader = (url: string) => Promise<unknown>

const importModule: UiModuleLoader = (url) => import(/* @vite-ignore */ url)
let loader: UiModuleLoader = importModule

export function loadUiModule(url: string): Promise<unknown> {
  return loader(url)
}

/** Tests only: jsdom cannot `import()` a URL. `null` restores the real loader. */
export function setUiModuleLoader(next: UiModuleLoader | null): void {
  loader = next ?? importModule
}
