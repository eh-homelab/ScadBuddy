import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { cleanTagUid, MIFARE_UID_SUFFIX } from './tagUid'

/**
 * #8042 (#7905): `cleanTagUid` against the shared vectors, which
 * `backend/tests/filament/test_tag_uid_vectors.py` runs against `clean_tag_uid` too, so the
 * browser cleans a UID as the server does.
 */
const { suffix, cases } = JSON.parse(readFileSync(`${import.meta.dirname}/../../../test-fixtures/filament/tag-uid-vectors.json`, 'utf8')) as {
  suffix: string
  cases: { in: string | null; out: string | null }[]
}

describe('tagUid', () => {
  it('cleanTagUid matches the vectors', () => {
    expect(MIFARE_UID_SUFFIX).toBe(suffix)
    for (const one of cases) expect(cleanTagUid(one.in), JSON.stringify(one)).toBe(one.out)
  })
})
