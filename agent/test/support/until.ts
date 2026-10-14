/** Poll until `check` holds, or fail after `timeoutMs`. */
export async function until(check: () => boolean | Promise<boolean>, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!(await check())) {
    if (Date.now() > deadline) throw new Error('until: the condition never held')
    await new Promise((r) => setTimeout(r, 10))
  }
}
