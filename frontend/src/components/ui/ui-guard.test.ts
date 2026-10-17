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

/** Every source file under src, the mocks and tests included, but not this one. */
const sources = files(src).filter((path) => /\.(?:tsx?|css)$/.test(path) && !path.endsWith('ui-guard.test.ts'))

function matches(pattern: RegExp): string[] {
  return sources.flatMap((path) => [...readFileSync(path, 'utf8').matchAll(pattern)].map((m) => `${relative(src, path)}: ${m[0]}`))
}

describe('the shadcn components (src/components/ui)', () => {
  it('share no name, ignoring case', () => {
    // On a case-insensitive file system (macOS, Windows) `./ui/Button` would find
    // button.tsx: the legacy Button/ and Dialog/ were the last such pair.
    const names = readdirSync(ui).map((name) => name.replace(/\.tsx?$/, '').toLowerCase())
    expect(names.filter((name, i) => names.indexOf(name) !== i)).toEqual([])
  })
})

/**
 * Every Tailwind 4 utility that takes a colour from the theme (`--color-*`), but `bg`:
 * the one where `accent` and `muted`, shadcn's surfaces, belong.
 */
const COLOUR_UTILITIES = [
  'text',
  'decoration',
  'border',
  'border-(?:[xytrblse]|bs|be)',
  'divide',
  'outline',
  'ring',
  'ring-offset',
  'inset-ring',
  'shadow',
  'inset-shadow',
  'drop-shadow',
  'text-shadow',
  'accent',
  'caret',
  'fill',
  'stroke',
  'placeholder',
  'from',
  'via',
  'to',
  // Mask gradient stops: mask-linear-from-*, mask-radial-to-*, mask-t-from-* …
  'mask(?:-[a-z]+)?-(?:from|to)',
]

/** `accent` or `muted` in one of them, with any opacity (`/70`, `/[0.7]`) and `!`. */
const NOT_A_SURFACE = new RegExp(
  `(?<![\\w-])(?:${COLOUR_UTILITIES.join('|')})-(?:accent|muted)(?:/(?:\\d+|\\[[^\\]]+\\]|\\([^)]+\\)))?!?(?![\\w-])`,
  'g',
)

describe('the colour names', () => {
  it('keeps no ui-accent or ui-muted, the names shadcn’s surfaces had during the conversion', () => {
    expect(matches(/(?<![\w-])[\w:-]*ui-(?:accent|muted)(?![\w-])/g)).toEqual([])
  })

  it('uses accent and muted only as shadcn’s surfaces, never as text or a line', () => {
    // `accent` was ScadBuddy's amber and `muted` its secondary text; both are shadcn's
    // surfaces now (index.css). The amber is `primary` and the secondary text `subtle`,
    // so a `text-muted` left over would be a surface colour used as text.
    expect(matches(NOT_A_SURFACE)).toEqual([])
  })

  it.each([
    'text-muted',
    'hover:text-accent',
    'text-muted/70',
    'text-muted/[0.7]',
    'text-muted!',
    'border-muted',
    'border-t-muted',
    'border-x-accent',
    'border-s-muted',
    'border-bs-muted',
    'divide-muted',
    'outline-accent',
    'ring-muted',
    'ring-offset-accent',
    'inset-ring-muted',
    'shadow-muted/20',
    'inset-shadow-accent',
    'drop-shadow-muted',
    'text-shadow-accent',
    'decoration-muted',
    'accent-muted',
    'caret-accent',
    'fill-muted',
    'stroke-accent',
    'placeholder-muted',
    'from-accent',
    'via-muted',
    'to-accent',
    'mask-linear-from-muted',
    'mask-radial-to-accent',
    'mask-t-from-muted',
  ])('catches %s', (name) => {
    expect(`<p className="x ${name} y">`.match(NOT_A_SURFACE)).toEqual([name.replace(/^\w+:/, '')])
  })

  it.each([
    'bg-muted',
    'hover:bg-muted/50',
    'bg-accent',
    'text-muted-foreground',
    'text-accent-foreground',
    'bg-sidebar-accent',
    'text-subtle',
    'text-primary',
    'border-border',
    'muted',
  ])('leaves %s alone', (name) => {
    expect(`<p className="x ${name} y">`.match(NOT_A_SURFACE)).toBeNull()
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
 * Each file allowed a match, by the pattern it may match, with why: a role set on a
 * shadcn component, not a control built by hand.
 */
const ALLOWED: Record<string, RegExp> = {
  // `role="alertdialog"` on the companion-aware DialogContent: Radix's AlertDialog is
  // always modal, and the assistant panel must stay usable beside a flow's confirmation
  // (#798), so it is the dialog primitive with the alert dialog's role.
  [join('components', 'flows', 'Confirm.tsx')]: HAND_ROLLED[0]!,
}

describe('no hand-rolled controls outside src/components/ui (#3653)', () => {
  it('finds none', () => {
    const found = files(src)
      .filter((path) => /\.tsx$/.test(path))
      .filter((path) => !path.startsWith(ui) && !/\.test\.tsx$/.test(path) && !path.includes(`${join(src, 'mocks')}`))
      .flatMap((path) => {
        const name = relative(src, path)
        const text = readFileSync(path, 'utf8')
        return HAND_ROLLED.filter((pattern) => pattern !== ALLOWED[name] && pattern.test(text)).map(
          (pattern) => `${name}: ${pattern.source}`,
        )
      })
    expect(found).toEqual([])
  })

  it('allows only files that still need it', () => {
    // An exception outlives its reason unnoticed otherwise.
    const stale = Object.entries(ALLOWED).filter(([name, pattern]) => !pattern.test(readFileSync(join(src, name), 'utf8')))
    expect(stale).toEqual([])
  })
})
