import { afterAll, beforeAll } from 'vitest'
import { startCredentialGateway, stopCredentialGateway } from '../../src/gateway/server.js'

// Every query reaches its model endpoint through the process's credential gateway
// (src/gateway/server.ts), as main.ts starts it, so each test file starts one. A
// test that points the API-key kinds at its fake restarts it with `anthropicUrl`.
beforeAll(async () => {
  await startCredentialGateway()
})

afterAll(async () => {
  await stopCredentialGateway()
})
