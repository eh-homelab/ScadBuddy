// The one-off host grant (#959): what tells the backend that a fetch off the asset
// allowlist was approved by a person. fetch_asset asks for it only as it runs, which is
// only after its call's approval (`allow_host` pins it outward, pinnedOutward.ts), and
// sends it in FETCH_GRANT_HEADER. The API mints it (`POST …/crud/agent/fetch-grants`,
// api/agentKeys.ts `CrudFetchGrants`, #5299 step 12) under a key derived from its KEK, and
// the asset route (`library/fetch_grants.py`) checks the MAC, the host, the very URL and
// the expiry, and spends its nonce, so a grant serves one fetch.
// A grant is a bearer secret: never log one.

export const FETCH_GRANT_HEADER = 'X-ScadBuddy-Fetch-Grant'

export type FetchGrantMinter = (host: string, url: string) => Promise<string>
