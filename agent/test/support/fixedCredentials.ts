import type { Credential } from '../../src/credentials.js'
import type { AttemptOutcome, CredentialSource, PooledCredential } from '../../src/harness/fallback.js'

/** One credential that is always usable, for tests of everything but the fallback; outcomes are kept in `reports`. */
export function fixedCredentials(credential: Credential): CredentialSource & { reports: AttemptOutcome[] } {
  const reports: AttemptOutcome[] = []
  const pooled: PooledCredential = { id: 'fixed', epoch: 0, label: 'the test credential', credential }
  return {
    reports,
    candidates: () => Promise.resolve([pooled]),
    reporter: () => (_attempt, outcome) => {
      reports.push(outcome)
      return Promise.resolve()
    },
  }
}
