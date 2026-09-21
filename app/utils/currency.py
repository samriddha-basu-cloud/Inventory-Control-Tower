"""Multi-currency valuation helpers."""


class CurrencyConversionError(Exception):
    pass


def convert(amount, from_currency, to_currency, rates=None):
    from_currency = (from_currency or "INR").upper()
    to_currency = (to_currency or "INR").upper()
    if from_currency == to_currency:
        return amount

    rates = rates or {}
    key = (from_currency, to_currency)
    if key in rates:
        return amount * rates[key]
    reverse_key = (to_currency, from_currency)
    if reverse_key in rates:
        return amount / rates[reverse_key]

    raise CurrencyConversionError(
        f"No exchange rate registered for {from_currency} -> {to_currency}."
    )


def build_rate_map(rate_rows):
    m = {}
    for row in rate_rows:
        m[(row.from_currency.upper(), row.to_currency.upper())] = row.rate
    return m
