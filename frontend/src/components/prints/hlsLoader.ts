/** Loads hls.js on first use, out of the entry chunk (#7330); a seam for the tests. */
export const hlsLoader = { load: () => import('hls.js') }
