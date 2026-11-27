import { FILE_STILL_MAX, stillOf } from '../scan/scanCamera'

/** The Shape Detection API's detector, where the browser has one (Chrome on Android). */
export interface BarcodeDetectorLike {
  detect(source: HTMLVideoElement): Promise<{ rawValue: string }[]>
}
export type BarcodeDetectorClass = new (options?: { formats: string[] }) => BarcodeDetectorLike

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
