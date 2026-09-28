from decimal import Decimal, ROUND_HALF_UP

from . import conf


def _kes(value):
    """M-Pesa STK push only accepts whole shillings."""
    return Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def quote(term_months, *, include_install_fee=False, monthly_price=None):
    discounts = conf.get("TERM_DISCOUNT_PERCENT")
    if term_months not in discounts:
        raise ValueError(f"Unsupported term: {term_months} months.")

    monthly = Decimal(monthly_price if monthly_price is not None else conf.get("MONTHLY_PRICE"))
    pct = Decimal(discounts[term_months])
    list_price = _kes(monthly * term_months)
    subscription = _kes(monthly * term_months * (Decimal(100) - pct) / Decimal(100))
    install = _kes(conf.get("INSTALL_FEE")) if include_install_fee else Decimal("0")

    return {
        "term_months": term_months,
        "list_price": list_price,
        "discount_percent": pct,
        "discount_amount": list_price - subscription,
        "subscription_amount": subscription,
        "install_fee": install,
        "total": subscription + install,
        "effective_monthly": (subscription / term_months).quantize(Decimal("0.01")),
        "currency": conf.get("CURRENCY"),
    }


def quote_table(*, include_install_fee=False, monthly_price=None):
    return [
        quote(t, include_install_fee=include_install_fee, monthly_price=monthly_price)
        for t in sorted(conf.get("TERM_DISCOUNT_PERCENT"))
    ]
