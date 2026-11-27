import { cleanTagUid } from './tagUid'

/** One data block as the ScadBuddy app's bridge gives it (`app/…/NfcBridge.cs`). */
interface BridgeBlock {
  index: number
  dataHex: string
}

/** The app's last MIFARE Classic read (`nfc-polyfill.js`, `window.ScadBuddyNfc.lastMifare`). */
interface BridgeMifare {
  uid: string
  blocks: BridgeBlock[]
}

declare global {
  interface Window {
    ScadBuddyNfc?: { native?: boolean; lastMifare?: BridgeMifare | null }
  }
}

/** A MIFARE read as `POST /tags/reads` takes it (#8148). */
export interface MifareRead {
  uid: string
  tag_type: 'mifare_classic'
  mifare_blocks: { index: number; data_hex: string }[]
}

const BLOCK = /^[0-9a-fA-F]{32}$/

/**
 * The app's MIFARE blocks for the tap whose serial number is `serial`, taken once: the app sets
 * `lastMifare` just before it hands the page the reading, and a later tap of another tag must not
 * post them. Null on a browser without the app, for an NDEF tag, or when the blocks are not the
 * shape the server takes; the read then posts its UID and records only.
 */
export function takeMifare(serial: string): MifareRead | null {
  const bridge = typeof window === 'undefined' ? undefined : window.ScadBuddyNfc
  const last = bridge?.lastMifare
  if (!bridge || !last || typeof last.uid !== 'string' || !Array.isArray(last.blocks)) return null
  if (cleanTagUid(last.uid) === null || cleanTagUid(last.uid) !== cleanTagUid(serial)) return null
  bridge.lastMifare = null
  const blocks = last.blocks.filter(
    (block) => Number.isInteger(block?.index) && block.index >= 0 && block.index < 64 && typeof block.dataHex === 'string' && BLOCK.test(block.dataHex),
  )
  if (blocks.length === 0 || blocks.length > 64) return null
  return { uid: last.uid, tag_type: 'mifare_classic', mifare_blocks: blocks.map((block) => ({ index: block.index, data_hex: block.dataHex })) }
}
