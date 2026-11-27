/** What the AMS reports after a 4-byte MIFARE UID (`spools/models.py` `MIFARE_UID_SUFFIX`, #8042). */
export const MIFARE_UID_SUFFIX = '00000100'

/**
 * `clean_tag_uid`, the browser's one copy: hex digits only, the last 16, upper case; null when
 * empty or all zeros. A 4-byte UID (8 hex digits) takes the AMS's form, the UID and then
 * `MIFARE_UID_SUFFIX`, so a phone's read and the AMS's name one tag (#8042). Both copies are held
 * to `test-fixtures/filament/tag-uid-vectors.json`.
 */
export function cleanTagUid(uid: string | null | undefined): string | null {
  const hex = (uid ?? '').replace(/[^0-9a-fA-F]/g, '').toUpperCase().slice(-16)
  if (!hex || /^0+$/.test(hex)) return null
  return hex.length === 8 ? hex + MIFARE_UID_SUFFIX : hex
}
