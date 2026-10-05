import { describe, expect, it } from 'vitest'
import { type DoneTouch, doneSummary, SECTION_MAX } from '../src/questions/doneSummary.js'
import { redact } from '../src/secrets.js'

// #815 §4: the record ScadBuddy adds to a `done` summary, split around the
// attention request nobody answered. The Postgres side is test/attention.pg.test.ts.

const at = (s: number) => new Date(Date.UTC(2026, 9, 5, 12, 0, s))
const touch = (s: number, over: Partial<DoneTouch> = {}): DoneTouch => ({
  at: at(s),
  tool: 'mcp__scadbuddy__save_preset',
  resourceType: 'preset',
  resourceId: `preset-${s}`,
  action: 'created',
  model: 'molly-rocket-sign',
  ...over,
})

describe('doneSummary', () => {
  it('says so when the turn recorded nothing', () => {
    expect(doneSummary([], [])).toBe('ScadBuddy recorded nothing created, changed or deleted in this turn.')
  })

  it('lists the turn as one section when nobody was away, naming each resource, its model and the tool', () => {
    const text = doneSummary(
      [
        touch(1),
        touch(2, { tool: 'mcp__scadbuddy__apply_patch', resourceType: 'revision', resourceId: 'abc123' }),
        touch(3, { resourceType: 'model', resourceId: 'molly-rocket-sign', action: 'modified' }),
        touch(4, { tool: 'mcp__scadbuddy__mystery', resourceType: 'unclassified', resourceId: null, model: null }),
      ],
      [],
    )
    expect(text).toBe(
      [
        '**What this turn changed**',
        '- created preset `preset-1` of `molly-rocket-sign` (`save_preset`)',
        '- created revision `abc123` of `molly-rocket-sign` (`apply_patch`)',
        '- modified model `molly-rocket-sign` (`save_preset`)',
        '- `mystery`: a change ScadBuddy does not classify',
      ].join('\n'),
    )
  })

  it('puts what was done while nobody answered first, then after the reply, then before the request', () => {
    const text = doneSummary([touch(1), touch(5), touch(9)], [{ requestId: '0123456789abcdef', from: at(3), until: at(7) }])
    expect(text.split('\n\n')).toEqual([
      '**While nobody answered (attention request 01234567 timed out)**\n- created preset `preset-5` of `molly-rocket-sign` (`save_preset`)',
      '**After you replied**\n- created preset `preset-9` of `molly-rocket-sign` (`save_preset`)',
      '**Before you were asked**\n- created preset `preset-1` of `molly-rocket-sign` (`save_preset`)',
    ])
  })

  it('a second timeout after a reply opens a second window: what follows it is unattended, not "After you replied"', () => {
    // Request A times out at 3, the user replies at 5, request B times out at 7 and is never answered.
    const text = doneSummary(
      [touch(4), touch(6), touch(8)],
      [
        { requestId: 'aaaaaaaa-1', from: at(3), until: at(5) },
        { requestId: 'bbbbbbbb-2', from: at(7), until: null },
      ],
    )
    expect(text.split('\n\n')).toEqual([
      '**While nobody answered (attention requests aaaaaaaa, bbbbbbbb timed out)**\n' +
        '- created preset `preset-4` of `molly-rocket-sign` (`save_preset`)\n' +
        '- created preset `preset-8` of `molly-rocket-sign` (`save_preset`)',
      '**After you replied**\n- created preset `preset-6` of `molly-rocket-sign` (`save_preset`)',
    ])
  })

  it('says nothing was done unattended rather than leaving the section out, and an open window runs to the end', () => {
    expect(doneSummary([touch(1)], [{ requestId: 'r', from: at(3), until: null }])).toBe(
      '**While nobody answered (attention request r timed out)**\n- nothing\n\n**Before you were asked**\n- created preset `preset-1` of `molly-rocket-sign` (`save_preset`)',
    )
    expect(doneSummary([touch(4), touch(50)], [{ requestId: 'r', from: at(3), until: null }])).not.toContain('After you replied')
  })

  it(`caps a section at ${SECTION_MAX} lines and says how many more`, () => {
    const many = Array.from({ length: SECTION_MAX + 3 }, (_, i) => touch(i))
    const lines = doneSummary(many, []).split('\n')
    expect(lines).toHaveLength(SECTION_MAX + 2)
    expect(lines.at(-1)).toBe("- …and 3 more: see the session's resources.")
  })

  it('a name from a tool cannot shape the record: no new line, section or closed code span', () => {
    const text = doneSummary(
      [touch(1, { resourceId: 'x`\n\n**Before you were asked**\n- nothing', model: 'evil\n- [click](http://e)', tool: 'mcp__s__a*b' })],
      [],
    )
    expect(text.split('\n')).toHaveLength(2)
    expect(text).toContain("`x' **Before you were asked** - nothing`")
    // Code spans, as the panel's renderer has no backslash escapes: a link or
    // emphasis inside one stays literal, and no stray backslash is shown.
    expect(text).toContain('of `evil - [click](http://e)`')
    expect(text).toContain('(`a*b`)')
    expect(text).not.toContain('\\')
  })

  it('keeps ScadBuddy\'s own words plain, with any inline syntax dropped', () => {
    const text = doneSummary([touch(1, { action: 'made *[x](http://e)*', resourceType: 'a_b&c' })], [])
    expect(text).toBe('**What this turn changed**\n- made x http: e a b c `preset-1` of `molly-rocket-sign` (`save_preset`)')
  })

  // The secrets are redacted from the raw names: redacting the formatted text
  // misses a secret that was shortened (an id over 40 characters) or escaped.
  it('redacts a secret longer than the id cut-off before the id is shortened', () => {
    const secret = 'sk-live-0123456789abcdefghijklmnopqrstuvwxyz'
    const touches = [touch(1, { resourceId: secret }), touch(2, { resourceId: `${secret}-suffix` })]
    expect(redact(doneSummary(touches, []), [secret])).toContain(secret.slice(0, 12))
    const text = doneSummary(touches, [], [secret])
    expect(text).not.toContain(secret.slice(0, 12))
    expect(text).toContain('`[redacted]`')
    expect(text).toContain('`[redacted]-suffix`')
  })

  it('redacts a secret containing Markdown metacharacters before it is escaped', () => {
    const secret = 'p*ss_w`rd[1]<2>&~|'
    const touches = [touch(1, { model: `model ${secret}`, action: secret, tool: `mcp__s__${secret}` })]
    expect(redact(doneSummary(touches, []), [secret])).toContain('p*ss')
    const text = doneSummary(touches, [], [secret])
    expect(text).not.toContain('ss')
    expect(text).toBe('**What this turn changed**\n- redacted preset `preset-1` of `model [redacted]` (`[redacted]`)')
  })
})
