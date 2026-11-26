import type { SavedBodies } from '../harness/httpRequest.js'

// A session's saved http_request bodies (#2374), in `ai_session_http_bodies`: any
// replica serves them, and a deploy keeps them, for as long as the session and its
// audit rows name them. The newest MAX_SAVED are kept, as on disk before. `CrudHttpBodies`
// (crudBlobs.ts) runs each method on the API's CRUD routes (area `http-bodies`, #5299 step 7,
// tools/go/core/internal/blobs), a ranged read as a ranged read.

/** A session's saved bodies, by its id: `CrudHttpBodies`. */
export type HttpBodiesFor = (sessionId: string) => SavedBodies
