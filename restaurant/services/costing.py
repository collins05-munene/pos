"""
Recipe costing and ingredient usage.  Pure calculations: nothing here changes stock.
"""
from decimal import Decimal, ROUND_HALF_UP

from ..models import MenuItem, Modifier, money

THREE = Decimal("0.001")


def q3(value):
    return Decimal(value).quantize(THREE, rounding=ROUND_HALF_UP)


def _recipe_or_none(menu_item):
    try:
        return menu_item.recipe
    except Exception:
        return None


def recipe_lines_usage(menu_item, portions=1, removed_ingredient_ids=()):
    """{ingredient: stock units} the recipe needs for `portions` portions (waste % included)."""
    recipe = _recipe_or_none(menu_item)
    usage = {}
    if recipe is None:
        return usage
    per_portion = Decimal(1) / Decimal(recipe.yield_portions)
    for rl in recipe.lines.select_related("ingredient__variant"):
        if rl.ingredient_id in removed_ingredient_ids:
            continue
        qty = (Decimal(rl.quantity) * per_portion * Decimal(portions)
               / Decimal(rl.ingredient.units_per_base)
               * (Decimal(1) + Decimal(rl.waste_percent) / Decimal(100)))
        usage[rl.ingredient] = usage.get(rl.ingredient, Decimal(0)) + qty
    return usage


def line_usage(menu_item, quantity, modifiers=()):
    """
    What stock one order line takes: {ProductVariant: quantity in that variant's own unit}.

    RECIPE   -> every recipe ingredient (minus REMOVE modifiers that name an ingredient)
    STOCKED  -> the item's own variant, one per portion
    NONE     -> nothing
    EXTRA modifiers that name an ingredient add that ingredient on top, whatever the item type.
    """
    modifiers = list(modifiers)
    removed = {m.ingredient_id for m in modifiers if m.kind == Modifier.Kind.REMOVE and m.ingredient_id}
    out = {}
    if menu_item.fulfilment == MenuItem.Fulfilment.RECIPE:
        for ing, qty in recipe_lines_usage(menu_item, quantity, removed).items():
            out[ing.variant] = out.get(ing.variant, Decimal(0)) + qty
    elif menu_item.fulfilment == MenuItem.Fulfilment.STOCKED:
        out[menu_item.variant] = Decimal(quantity)
    for m in modifiers:
        if m.kind == Modifier.Kind.EXTRA and m.ingredient_id and Decimal(m.ingredient_qty) > 0:
            ing = m.ingredient
            qty = Decimal(m.ingredient_qty) * Decimal(quantity) / Decimal(ing.units_per_base)
            out[ing.variant] = out.get(ing.variant, Decimal(0)) + qty
    return {v: q3(q) for v, q in out.items() if q3(q) > 0}


def recipe_cost(menu_item):
    """Cost of ONE portion from today's ingredient cost prices (waste % included)."""
    if menu_item.fulfilment != MenuItem.Fulfilment.RECIPE:
        return money(menu_item.variant.cost_price)
    total = Decimal(0)
    for ing, qty in recipe_lines_usage(menu_item, 1).items():
        total += qty * Decimal(ing.variant.cost_price)
    return money(total)


def modifier_cost(modifier):
    if modifier.kind == Modifier.Kind.EXTRA and modifier.ingredient_id:
        ing = modifier.ingredient
        return money(Decimal(modifier.ingredient_qty) / Decimal(ing.units_per_base) * Decimal(ing.variant.cost_price))
    return Decimal("0.00")


def cost_breakdown(menu_item):
    """Rows for the recipe screen: ingredient, quantity per portion, cost per portion; plus totals."""
    rows, total = [], Decimal(0)
    recipe = _recipe_or_none(menu_item)
    if recipe is not None:
        per_portion = Decimal(1) / Decimal(recipe.yield_portions)
        for rl in recipe.lines.select_related("ingredient__variant__product"):
            qty = Decimal(rl.quantity) * per_portion * (Decimal(1) + Decimal(rl.waste_percent) / Decimal(100))
            cost = qty / Decimal(rl.ingredient.units_per_base) * Decimal(rl.ingredient.variant.cost_price)
            total += cost
            rows.append({"line": rl, "qty": q3(qty), "cost": money(cost)})
    price = Decimal(menu_item.variant.retail_price)
    total = money(total)
    return {
        "rows": rows,
        "cost": total,
        "price": price,
        "profit": money(price - total),
        "food_cost_percent": (money(total / price * 100) if price > 0 else None),
    }


def refresh_menu_cost(menu_item):
    """Write today's recipe cost onto the menu item's variant so sales, COGS and profit use it."""
    cost = recipe_cost(menu_item)
    v = menu_item.variant
    if Decimal(v.cost_price) != cost:
        v.cost_price = cost
        v.save(update_fields=["cost_price"])
    return cost


def refresh_all_costs():
    n = 0
    for item in MenuItem.objects.filter(fulfilment=MenuItem.Fulfilment.RECIPE).select_related("variant"):
        before = item.variant.cost_price
        if refresh_menu_cost(item) != before:
            n += 1
    return n
