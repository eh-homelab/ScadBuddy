/** Template UI modules the msw API serves (spec 2026-09-27 §4). */
export const UI_MODULES: Record<string, Record<string, string>> = {
  'ui-demo': {
    'index.js': `
import { GREETING } from './greeting.js'
export function mount(root, host) {
  const title = document.createElement('p')
  title.textContent = GREETING
  title.setAttribute('data-testid', 'ui-demo-greeting')
  const param = document.createElement('sb-param')
  param.setAttribute('name', 'name')
  const touch = document.createElement('button')
  touch.textContent = 'Remember me'
  touch.onclick = () => host.inputs.set({ demo: { touched: true } })
  root.append(title, param, touch)
  host.describe(() => 'the demo UI')
  return () => root.replaceChildren()
}
`,
    'greeting.js': `export const GREETING = 'Hello from the template'\n`,
  },
  'ui-broken': {
    'index.js': `export function mount() { throw new Error('the template UI is broken on purpose') }\n`,
  },
}
