import { ApiError } from '../../../api/client'

/** Why a call failed, in words a person can act on: the API's detail, the error's message, or the fallback. */
export const reasonOf = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail : cause instanceof Error && cause.message ? cause.message : fallback)
