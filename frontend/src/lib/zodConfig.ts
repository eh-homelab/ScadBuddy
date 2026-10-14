import { z } from 'zod'

// The page CSP has no 'unsafe-eval'. zod's JIT probes `new Function` on its first parse
// and swallows the refusal, but the browser still reports a violation; jitless skips the
// probe (and the JIT it would enable). zod reads it when a schema is built, so this must
// run before any module that builds one at import: vite.config.ts puts this module in
// zod's own chunk, which every such chunk imports first. Imported by main.tsx only so
// the build keeps it.
z.config({ jitless: true })
