from decimal import Decimal

from django import template

from ..reporting import fmt_money

register = template.Library()


@register.filter
def money(value):
    return fmt_money(value)


@register.filter
def is_num(value):
    if isinstance(value, (Decimal, int, float)) and not isinstance(value, bool):
        return True
    return isinstance(value, str) and (value.endswith("%") and value[:-1].replace(".", "", 1).lstrip("+-").isdigit())


@register.filter
def is_dec(value):
    return isinstance(value, Decimal)


@register.filter
def neg(value):
    try:
        return Decimal(value) < 0
    except Exception:
        return False


@register.simple_tag(takes_context=True)
def qs_without(context, *drop):
    """Current query string minus the given keys (for pagination links)."""
    q = context["request"].GET.copy()
    for k in drop:
        q.pop(k, None)
    s = q.urlencode()
    return s + "&" if s else ""
