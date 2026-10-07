import { z } from 'zod'

// The page CSP has no 'unsafe-eval'. zod's JIT probes `new Function` on its first parse
// and swallows the refusal, but the browser still reports a violation; jitless skips the
// probe (and the JIT it would enable). Its own module, imported first by main.tsx, so it
// runs before any module that builds a schema at import.
z.config({ jitless: true })
