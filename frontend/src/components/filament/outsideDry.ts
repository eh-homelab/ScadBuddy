/** The hours a spool has spent outside dry storage, in words. */
export function outsideDryText(hours: number): string {
  const rounded = Math.round(hours * 10) / 10
  if (rounded <= 0) return 'No time outside dry storage'
  return `${rounded} ${rounded === 1 ? 'hour' : 'hours'} outside dry storage`
}
