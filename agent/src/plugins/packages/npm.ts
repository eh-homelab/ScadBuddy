import { createHash } from 'node:crypto'
import { PluginError } from '../registry.js'
import { ARCHIVE_LIMITS, type Downloader, MARKETPLACE_LIMITS } from './download.js'
import type { PackageSource } from './source.js'

// An npm source's tarball (#6935): a registry package is resolved to its version's
// tarball with the registry's version document, `GET <registry>/<name>/<version or tag>`
// (https://github.com/npm/registry/blob/main/docs/REGISTRY-API.md, "Getting a single
// version"), and the tarball is checked against the document's `dist.integrity` (SRI,
// https://www.w3.org/TR/SRI/), or its `dist.shasum` (SHA-1, hex) when it has none; a
// document with neither is refused, so no registry tarball is taken unchecked. A tarball
// link is fetched as it is (its sha256 is the pin either way). Nothing is installed and no script runs: the tarball is only
// unpacked (unpack.ts), as Claude Code does ("unpacked without running install
// scripts", https://code.claude.com/docs/en/plugins/marketplace-reference.md).

export type NpmTarball = { bytes: Buffer; tarballUrl: string }

function integrityMatches(bytes: Buffer, integrity: string): boolean {
  const known = integrity
    .split(/\s+/)
    .map((token) => /^(sha512|sha384|sha256)-([A-Za-z0-9+/=]+)$/.exec(token))
    .filter((m): m is RegExpExecArray => m !== null)
  if (!known.length) throw new PluginError(`the npm registry's integrity "${integrity}" names no sha256, sha384 or sha512`, 502)
  return known.some(([, algorithm, digest]) => createHash(algorithm!).update(bytes).digest('base64') === digest)
}

export async function fetchNpmTarball(
  source: Extract<PackageSource, { kind: 'npm' }>,
  download: Downloader,
  signal?: AbortSignal,
): Promise<NpmTarball> {
  if (source.ref === '') {
    const got = await download(source.url, { what: 'npm tarball', ...ARCHIVE_LIMITS, signal })
    return { bytes: got.bytes, tarballUrl: source.url }
  }
  const doc = await download(`${source.url}/${encodeURIComponent(source.ref)}`, {
    what: 'npm registry',
    ...MARKETPLACE_LIMITS,
    accept: 'application/json',
    signal,
  })
  let parsed: unknown
  try {
    parsed = JSON.parse(doc.bytes.toString('utf8'))
  } catch {
    throw new PluginError('the npm registry did not answer JSON', 502)
  }
  const dist = (parsed as { dist?: { tarball?: unknown; integrity?: unknown; shasum?: unknown } } | null)?.dist
  if (typeof dist?.tarball !== 'string') throw new PluginError(`the npm registry has no tarball for ${source.url} ${source.ref}`, 502)
  if (typeof dist.integrity !== 'string' && typeof dist.shasum !== 'string') {
    throw new PluginError(`the npm registry's document for ${source.url} ${source.ref} has neither an integrity nor a shasum`, 502)
  }
  const tarball = await download(dist.tarball, { what: 'npm tarball', ...ARCHIVE_LIMITS, signal })
  if (typeof dist.integrity === 'string') {
    if (!integrityMatches(tarball.bytes, dist.integrity)) throw new PluginError('the npm tarball does not match the registry\'s integrity', 502)
  } else {
    if (createHash('sha1').update(tarball.bytes).digest('hex') !== String(dist.shasum).toLowerCase()) {
      throw new PluginError('the npm tarball does not match the registry\'s shasum', 502)
    }
  }
  return { bytes: tarball.bytes, tarballUrl: dist.tarball }
}
