// As `orders/machine_rate.py` takes them: hours, watts and minutes to 0.1, money to 0.0001.
export const TENTHS = 1
export const AMOUNT = 4
export const decimal = (text: string, places: number) => new RegExp(`^\\d+(\\.\\d{1,${places}})?$`).test(text.trim())
export const placesText = (places: number) => (places === TENTHS ? 'with at most 1 decimal place, such as 12.5' : 'with at most 4 decimal places, such as 0.1234')
