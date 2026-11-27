import { useState } from 'react'
import { ApiError } from '../../../api/client'
import { createSpool, postTagRead, type NdefRecord, type Spool, type TagReadResult } from '../../../api/filament'
import { useAsync } from '../../../lib/useAsync'
import { entityApi, type PropertyValue } from '../../entities/api'
import { fieldErrorsOf, propertiesPatch, refusalText, type FieldErrors } from '../../entities/definedProperties'

const reasonOf = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail : fallback)

/**
 * #7273, #7167 — make a spool from a new tag's draft and bind the tag to it, as the scan page's
 * new-tag card and the home-return dialog both do. The spool's required properties (#7703) are
 * filled from `defined` (show `DefinedFields` with `schema` while nothing is `made`). When the
 * bind fails, the made spool is kept: the next `create` binds that spool and makes no second one.
 * `create` answers the read after the bind (it holds the spool), or null when it did not go through.
 */
export function useSpoolFromTag(uid: string, records: NdefRecord[], draft: TagReadResult['draft'] | undefined) {
  const [made, setMade] = useState<Spool | null>(null)
  const keys = useAsync(() => entityApi.propertySchema('spool'), [])
  const schema = keys.data?.schema ?? {}
  const [defined, setDefined] = useState<Record<string, PropertyValue | undefined>>({})
  const [fieldErrors, setFieldErrors] = useState<FieldErrors | undefined>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function create(): Promise<TagReadResult | null> {
    if (!draft && !made) return null
    setBusy(true)
    setError(null)
    setFieldErrors(undefined)
    let spool = made
    try {
      const properties = propertiesPatch(schema, {}, defined)
      spool ??= await createSpool(Object.keys(properties).length > 0 ? { ...draft!, properties } : draft!)
    } catch (cause) {
      const refused = fieldErrorsOf(cause)
      setFieldErrors(refused ?? undefined)
      setError(refused ? refusalText(refused) : reasonOf(cause, 'ScadBuddy could not make the spool. Try again.'))
      setBusy(false)
      return null
    }
    try {
      return await postTagRead({ uid, tag_type: null, records, bind_spool_id: spool.id })
    } catch (cause) {
      setMade(spool)
      setError(`Spool #${spool.number} was made, but the tag was not bound to it: ${reasonOf(cause, 'the bind failed')}. Tap "Bind to spool #${spool.number}" to try again.`)
      return null
    } finally {
      setBusy(false)
    }
  }

  return { schema, defined, setDefined, fieldErrors, made, busy, error, create }
}
