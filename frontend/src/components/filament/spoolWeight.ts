/**
 * A weight or another amount typed in plain digits, with an optional decimal point: no sign,
 * exponent, hex, grouping commas or unit, which `Number()` would take or turn into `NaN`.
 * The spool page and the scan page's weight field (#7273) both check typed grams with it.
 */
export const DECIMAL = /^\d+(\.\d+)?$/
