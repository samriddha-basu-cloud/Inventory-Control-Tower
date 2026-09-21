import pytest
from app.utils.uom import convert as uom_convert, UomConversionError, build_conversion_map
from app.utils.currency import convert as fx_convert, CurrencyConversionError, build_rate_map


class Row:
    def __init__(self, from_uom, to_uom, factor):
        self.from_uom, self.to_uom, self.factor = from_uom, to_uom, factor


class RateRow:
    def __init__(self, from_currency, to_currency, rate):
        self.from_currency, self.to_currency, self.rate = from_currency, to_currency, rate


def test_uom_same_unit_passthrough():
    assert uom_convert(10, "EA", "EA") == 10


def test_uom_conversion_forward():
    conversions = build_conversion_map([Row("CASE", "EA", 24)])
    assert uom_convert(2, "CASE", "EA", conversions) == 48


def test_uom_conversion_reverse():
    conversions = build_conversion_map([Row("CASE", "EA", 24)])
    assert uom_convert(48, "EA", "CASE", conversions) == 2


def test_uom_missing_conversion_raises():
    with pytest.raises(UomConversionError):
        uom_convert(10, "PALLET", "EA", {})


def test_currency_same_currency_passthrough():
    assert fx_convert(100, "INR", "INR") == 100


def test_currency_conversion_forward():
    rates = build_rate_map([RateRow("USD", "INR", 83.0)])
    assert fx_convert(10, "USD", "INR", rates) == 830.0


def test_currency_conversion_reverse():
    rates = build_rate_map([RateRow("USD", "INR", 83.0)])
    assert abs(fx_convert(830, "INR", "USD", rates) - 10) < 0.001


def test_currency_missing_rate_raises():
    with pytest.raises(CurrencyConversionError):
        fx_convert(10, "EUR", "JPY", {})
