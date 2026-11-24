import { lazy } from 'react'

/**
 * The shared 3D viewer. three.js is most of it: its own chunk, fetched once there is a
 * preview to show, by a template listing's page and a file listing's alike.
 */
export const Preview = lazy(async () => ({ default: (await import('../components/Preview')).Preview }))
