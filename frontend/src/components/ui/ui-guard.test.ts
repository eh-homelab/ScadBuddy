/// <reference types="node" />
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

// The shadcn/ui conversion (#3653). Read from disk: these are checks on the source.
const src = join(import.meta.dirname, '..', '..')
const ui = join(src, 'components', 'ui')

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    return statSync(path).isDirectory() ? files(path) : [path]
  })
}

/** The shadcn components: lower-case files, as the CLI names them. */
const generated = readdirSync(ui).filter((name) => /^[a-z][\w-]*\.tsx$/.test(name))

describe('the shadcn components (src/components/ui)', () => {
  it('use ui-accent and ui-muted for shadcn’s accent and muted surfaces', () => {
    // index.css: `bg-accent` is still ScadBuddy's amber and `text-muted` its secondary
    // text while unconverted pages use them; a component added with `shadcn add` comes
    // with shadcn's meaning and must be rewritten.
    const clashes = generated.flatMap((name) =>
      [...readFileSync(join(ui, name), 'utf8').matchAll(/(?<![\w-])bg-(accent|muted)(?![\w-])/g)].map(
        (m) => `${name}: ${m[0]}`,
      ),
    )
    expect(clashes).toEqual([])
  })
})

/**
 * A control the shadcn components provide, built by hand: a dialog, a menu, a listbox, a
 * checkbox or a native select. Matched in the source, so a hand-rolled one is caught
 * whatever it renders.
 */
const HAND_ROLLED = [
  /role=["'{`]+(?:dialog|alertdialog|menu|menuitem|menuitemcheckbox|menuitemradio|listbox|option|checkbox|radio|radiogroup|switch|tablist|tab|tooltip)\b/,
  /aria-modal=/,
  /type=["'](?:checkbox|radio)["']/,
  /<select\b/,
  /<dialog\b/,
  /<details\b/,
]

/**
 * TODO(#3653): turn on when the last area's conversion lands. Until then it would fail
 * on every page not yet converted; run it by hand to see what is left.
 */
const GUARD_ON = false

describe.skipIf(!GUARD_ON)('no hand-rolled controls outside src/components/ui (#3653)', () => {
  it('finds none', () => {
    const found = files(src)
      .filter((path) => /\.tsx$/.test(path))
      .filter((path) => !path.startsWith(ui) && !/\.test\.tsx$/.test(path) && !path.includes(`${join(src, 'mocks')}`))
      .flatMap((path) => {
        const text = readFileSync(path, 'utf8')
        return HAND_ROLLED.filter((pattern) => pattern.test(text)).map((pattern) => `${relative(src, path)}: ${pattern.source}`)
      })
    expect(found).toEqual([])
  })
})
