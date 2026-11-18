/** The hours a spool has spent outside dry storage, in words. */
export function outsideDryText(hours: number): string {
  if (hours <= 0) return 'No time outside dry storage'
  return `${hours} ${hours === 1 ? 'hour' : 'hours'} outside dry storage`
}
