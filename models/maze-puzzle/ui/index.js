// ScadBuddy template UI, host API v1 (docs/superpowers/specs/2026-09-27-template-pipelines-design.md §4):
// the generated form, with the lid's colour shown only when there is a lid.

const LID_ONLY = new Set(['lid_color'])

// The app's stylesheet reaches this shadow root, but only the utility classes the app
// itself uses exist in it, so the layout is the template's own. Colours are the app's
// theme tokens, which cross the shadow boundary.
const CSS = `
.form { display: flex; flex-direction: column; }
.group { padding: 12px 12px 0; font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.05em; color: var(--sb-faint); }
sb-param { display: block; }
sb-param[hidden] { display: none; }
`

function modeOf(inputs, schema) {
  const mode = inputs.params?.mode
  if (typeof mode === 'string') return mode
  return schema.parameters.find((param) => param.name === 'mode')?.initial
}

export async function mount(root, host) {
  const schema = await host.schema()
  const view = document.createElement('div')
  const style = document.createElement('style')
  style.textContent = CSS
  const form = document.createElement('div')
  form.className = 'form'
  let group = null
  for (const param of schema.parameters) {
    const name = param.group || 'Parameters'
    if (name !== group) {
      group = name
      const heading = document.createElement('h3')
      heading.className = 'group'
      heading.textContent = name
      form.append(heading)
    }
    const field = document.createElement('sb-param')
    field.setAttribute('name', param.name)
    form.append(field)
  }
  view.append(style, form)
  root.append(view)

  function draw(inputs) {
    const lid = modeOf(inputs, schema) === 'ball_lid'
    for (const field of form.querySelectorAll('sb-param')) {
      field.hidden = LID_ONLY.has(field.getAttribute('name')) && !lid
    }
  }
  draw(host.inputs.get())
  const unsubscribe = host.inputs.subscribe(draw)
  host.describe(() =>
    modeOf(host.inputs.get(), schema) === 'ball_lid'
      ? 'Ball maze with a snap-on lid; lid_color is shown.'
      : 'Open-tray ball maze; lid_color is hidden because there is no lid.',
  )
  return () => {
    unsubscribe()
    view.remove()
  }
}
