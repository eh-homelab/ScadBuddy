/**
 * The "Edit in ScadBuddy" deep link, mirrored from `scadbuddy/library/deeplink.py`.
 * The backend stamps the absolute form into every 3MF and attaches it to the file
 * Bambuddy holds; the app only ever needs the path.
 */
export function editPath(outputId: string): string {
  return `/edit/${outputId}`
}
