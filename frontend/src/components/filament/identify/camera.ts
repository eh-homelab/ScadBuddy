import { FILE_STILL_MAX, stillOf } from '../scan/scanCamera'

/** The Shape Detection API's detector, where the browser has one (Chrome on Android). */
export interface BarcodeDetectorLike {
  detect(source: HTMLVideoElement): Promise<{ rawValue: string }[]>
}
export type BarcodeDetectorClass = (new (options?: { formats: string[] }) => BarcodeDetectorLike) & { getSupportedFormats?: () => Promise<string[]> }

/** A ScadBuddy label's QR code. */
export const QR_FORMATS: readonly string[] = ['qr_code']

/** #8372: a spool's ScadBuddy QR code, or the maker's barcode on its box (EAN, UPC). */
export const SPOOL_FORMATS: readonly string[] = ['qr_code', 'ean_13', 'ean_8', 'upc_a', 'upc_e']

/** The formats the browser's detector reads, of the wanted ones; all of them where it does not say. */
export async function readableFormats(Detector: BarcodeDetectorClass, wanted: readonly string[]): Promise<string[]> {
  let supported: string[] | null = null
  try {
    supported = (await Detector.getSupportedFormats?.()) ?? null
  } catch {
    // The detector does not say: ask for every wanted format.
  }
  const kept = supported ? wanted.filter((format) => supported.includes(format)) : [...wanted]
  return kept.length > 0 ? kept : [...wanted]
}

export const detectorClass = (): BarcodeDetectorClass | null => (window as unknown as { BarcodeDetector?: BarcodeDetectorClass }).BarcodeDetector ?? null

/** A label's photo drawn again as a JPEG of a known size (core takes JPEG, PNG or WebP; a phone may give HEIC). */
export async function jpegOf(file: File): Promise<Blob> {
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    throw new Error('This browser cannot open that photo. Type the code from the label.')
  }
  try {
    return await stillOf(bitmap, bitmap.width, bitmap.height, FILE_STILL_MAX)
  } finally {
    bitmap.close()
  }
}
