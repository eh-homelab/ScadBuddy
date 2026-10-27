/// <reference types="node" />
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { expect, it } from 'vitest'

// #4873 — index.html's inline entry-reload handler runs only if PAGE_CSP carries its
// hash, and a wrong hash blocks it silently. Vite copies the inline script into
// dist/index.html byte for byte, so the source's hash is the served one;
// e2e/entry-reload.spec.ts proves it under the preview's PAGE_CSP. The backend's copy
// of the policy is held equal to page-csp.txt by test_the_page_carries_the_csp and CI.
const html = readFileSync(`${import.meta.dirname}/../index.html`, 'utf8')
const csp = readFileSync(`${import.meta.dirname}/../page-csp.txt`, 'utf8')

it("PAGE_CSP's script-src hash is index.html's inline script", () => {
  const inline = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]!)
  expect(inline).toHaveLength(1)
  const hash = createHash('sha256').update(inline[0]!, 'utf8').digest('base64')
  const scriptSrc = /script-src ([^;]*);/.exec(csp)?.[1]
  expect(scriptSrc, `put 'sha256-${hash}' in page-csp.txt and PAGE_CSP`).toBe(`'self' 'sha256-${hash}'`)
})
