/** Remembers a page's Advanced toggle (`lib/advanced.ts` `useAdvanced`) as on before the page mounts. */
export function rememberAdvanced(key: string, on = true) {
  window.localStorage.setItem(key, on ? '1' : '0')
}
