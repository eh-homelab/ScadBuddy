import type { TagBind } from '../../api/filament'

export const KINDS: { id: TagBind['kind'] & string; name: string }[] = [
  { id: 'bambu_mifare', name: 'Bambu Lab (RFID)' },
  { id: 'ntag_openspool', name: 'OpenSpool (NTAG)' },
  { id: 'ntag_opentag', name: 'OpenTag (NTAG)' },
  { id: 'other', name: 'Other' },
]
