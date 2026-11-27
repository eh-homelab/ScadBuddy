import { afterEach, describe, expect, it } from 'vitest'
import { takeMifare } from './nativeMifare'

const block = (index: number) => ({ index, dataHex: '4131372d503100004746413137000000' })

describe('takeMifare', () => {
  afterEach(() => {
    delete window.ScadBuddyNfc
  })

  it('takes the app’s blocks for the tap of the same tag, once', () => {
    window.ScadBuddyNfc = { native: true, lastMifare: { uid: '9CC69CE3', blocks: [block(1), block(2)] } }
    // The app hands the page the AMS's form of the serial number.
    expect(takeMifare('9c:c6:9c:e3:00:00:01:00')).toEqual({
      uid: '9CC69CE3',
      tag_type: 'mifare_classic',
      mifare_blocks: [
        { index: 1, data_hex: '4131372d503100004746413137000000' },
        { index: 2, data_hex: '4131372d503100004746413137000000' },
      ],
    })
    expect(takeMifare('9c:c6:9c:e3:00:00:01:00')).toBeNull()
  })

  it('leaves another tag’s blocks alone', () => {
    window.ScadBuddyNfc = { native: true, lastMifare: { uid: 'DCAE1CE1', blocks: [block(1)] } }
    expect(takeMifare('04:a2:b3:c4')).toBeNull()
    expect(window.ScadBuddyNfc.lastMifare).not.toBeNull()
  })

  it('drops a block that is not 16 bytes at an index from 0 to 63', () => {
    window.ScadBuddyNfc = {
      native: true,
      lastMifare: { uid: '9CC69CE3', blocks: [block(1), { index: 64, dataHex: block(1).dataHex }, { index: 2, dataHex: '00' }] },
    }
    expect(takeMifare('9CC69CE3')?.mifare_blocks.map((b) => b.index)).toEqual([1])
  })

  it('is null in a browser without the app', () => {
    expect(takeMifare('9CC69CE3')).toBeNull()
  })
})
