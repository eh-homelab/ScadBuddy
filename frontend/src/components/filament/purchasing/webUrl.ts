/** `url` as a normalised address only when it is an `http:` or `https:` one, else `null`: a link from stored text must never run script. */
export function webUrl(url: string | null | undefined): string | null {
  if (!url) return null
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null
  } catch {
    return null
  }
}

/** What a typed website or product address needs, or `null` when it is empty or fine. */
export function webUrlError(text: string, what: string): string | null {
  const value = text.trim()
  if (!value) return null
  return webUrl(value) ? null : `${what} must start with http:// or https://.`
}
