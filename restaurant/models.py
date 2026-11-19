"""
Restaurant / hotel module.

Design rules (so it plugs into the existing POS instead of running beside it):

* Every dish, drink and extra IS a normal ProductVariant (MenuItem is a 1:1 extension).  That keeps
  prices, discounts, receipts, refunds and reports working unchanged.
* Every ingredient IS a normal base ProductVariant (Ingredient is a 1:1 extension).  Buying,
  counting, transferring and low-stock alerts therefore reuse Purchase Orders, Stock Management
  and inventory.stock.change_stock -- the only place stock quantities change.
* A restaurant "check" (RestaurantOrder) lives here while it is being served.  When it is fully paid
  it is turned into an ordinary sales.Order (+ Receipt), so shifts, cash sessions, the sales
  dashboard and refunds all see it.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from inventory.models import Branch
from products.models import ProductVariant
from tenants.models import TenantOwnedModel

TWO = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value):
    return Decimal(value).quantize(TWO, rounding=ROUND_HALF_UP)


# =====================================================================================
# Settings (one row per restaurant)
# =====================================================================================
class RestaurantSettings(TenantOwnedModel):
    class Deduct(models.TextChoices):
        ON_SEND = "ON_SEND", "When the order is sent to the kitchen"
        ON_READY = "ON_READY", "When the kitchen marks it ready"
        ON_SERVED = "ON_SERVED", "When it is served to the guest"
        ON_PAID = "ON_PAID", "When the bill is paid"

    deduction_policy = models.CharField(
        max_length=10, choices=Deduct.choices, default=Deduct.ON_READY,
        help_text="When ingredients (or stocked drinks) are taken out of stock for an order line.")
    allow_negative_stock = models.BooleanField(
        default=True,
        help_text="Keep serving when the count says an ingredient is out (stock goes negative and "
                  "managers are alerted). Off = the kitchen is blocked until stock is fixed.")
    service_charge_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    service_charge_dine_in_only = models.BooleanField(default=True)
    default_delivery_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    kds_late_minutes = models.PositiveIntegerField(
        default=15, help_text="A kitchen ticket turns red after this many minutes.")
    accept_mpesa = models.BooleanField(
        default=False, help_text="Let cashiers record M-Pesa payments (typed in with the M-Pesa code).")
    accept_card = models.BooleanField(default=False, help_text="Let cashiers record card payments.")
    online_ordering_enabled = models.BooleanField(default=False)
    auto_accept_online = models.BooleanField(
        default=False, help_text="Online orders go straight to the kitchen. Off = staff accept them first.")
    online_key_id = models.CharField(max_length=12, blank=True, db_index=True)
    online_key_hash = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Restaurant settings"
        constraints = [models.UniqueConstraint(fields=["tenant"], name="uniq_restaurant_settings_per_tenant")]

    def __str__(self):
        return f"Restaurant settings for {self.tenant_id}"


def get_restaurant_settings(tenant=None):
    from tenants.context import get_current_tenant
    tenant = tenant or get_current_tenant()
    if tenant is None:
        return RestaurantSettings()
    obj = RestaurantSettings.all_objects.filter(tenant=tenant).first()
    if obj is None:
        obj, _ = RestaurantSettings.all_objects.get_or_create(tenant=tenant)
    return obj


# =====================================================================================
# Floor plan
# =====================================================================================
class Station(TenantOwnedModel):
    """A place food or drinks are made: Grill, Pizza, Bar ... Every menu item belongs to one."""
    name = models.CharField(max_length=60)
    color = models.CharField(max_length=7, default="#2563eb")
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="uniq_station_name_per_tenant")]

    def __str__(self):
        return self.name


class FloorZone(TenantOwnedModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="floor_zones")
    name = models.CharField(max_length=60)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [models.UniqueConstraint(fields=["tenant", "branch", "name"], name="uniq_zone_per_branch")]

    def __str__(self):
        return self.name


class Table(TenantOwnedModel):
    class Shape(models.TextChoices):
        SQUARE = "SQUARE", "Square"
        ROUND = "ROUND", "Round"
        RECT = "RECT", "Rectangle"

    class Hold(models.TextChoices):
        NONE = "NONE", "Normal"
        RESERVED = "RESERVED", "Reserved"
        CLEANING = "CLEANING", "Being cleaned"
        BLOCKED = "BLOCKED", "Out of service"

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="tables")
    zone = models.ForeignKey(FloorZone, null=True, blank=True, on_delete=models.SET_NULL, related_name="tables")
    name = models.CharField(max_length=30)
    seats = models.PositiveSmallIntegerField(default=4)
    shape = models.CharField(max_length=8, choices=Shape.choices, default=Shape.SQUARE)
    # Position and size on the floor plan, as percentages of the plan so it scales to any screen.
    x = models.FloatField(default=5)
    y = models.FloatField(default=5)
    w = models.FloatField(default=9)
    h = models.FloatField(default=9)
    hold = models.CharField(max_length=10, choices=Hold.choices, default=Hold.NONE)
    reserved_for = models.CharField(max_length=80, blank=True)
    waiter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="assigned_tables")
    merged_into = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="merged_tables",
                                    help_text="Set on a table that has been pushed together with another.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["zone__sort_order", "name"]
        constraints = [models.UniqueConstraint(fields=["tenant", "branch", "name"], name="uniq_table_per_branch")]

    def __str__(self):
        return self.name

    @property
    def capacity(self):
        return self.seats + sum(t.seats for t in self.merged_tables.all())

    @property
    def label(self):
        kids = [t.name for t in self.merged_tables.all()]
        return "+".join([self.name] + kids) if kids else self.name


# =====================================================================================
# Menu, ingredients, recipes
# =====================================================================================
class MenuItem(TenantOwnedModel):
    class Kind(models.TextChoices):
        MEAL = "MEAL", "Meal"
        DRINK = "DRINK", "Drink"
        EXTRA = "EXTRA", "Extra / side"

    class Fulfilment(models.TextChoices):
        RECIPE = "RECIPE", "Made from a recipe (uses ingredients)"
        STOCKED = "STOCKED", "Resold from stock (e.g. bottled soda)"
        NONE = "NONE", "No stock (service, coffee refills ...)"

    variant = models.OneToOneField(ProductVariant, on_delete=models.PROTECT, related_name="menu_item")
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.MEAL)
    fulfilment = models.CharField(max_length=8, choices=Fulfilment.choices, default=Fulfilment.RECIPE)
    station = models.ForeignKey(Station, null=True, blank=True, on_delete=models.SET_NULL, related_name="items")
    prep_minutes = models.PositiveSmallIntegerField(default=10)
    is_available = models.BooleanField(default=True, help_text="Untick to '86' an item that has run out.")
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "variant__product__name"]

    def __str__(self):
        return self.name

    @property
    def name(self):
        return self.variant.product.name

    @property
    def price(self):
        return self.variant.retail_price

    @property
    def category(self):
        return self.variant.product.category


class Ingredient(TenantOwnedModel):
    """
    Marks a base ProductVariant as a kitchen ingredient.

    Stock is held in the variant's own unit (say g, ml or pcs). Recipes may be written in a different
    `recipe_unit`; recipe quantity / units_per_base = stock units used.  Keep it simple: stock
    ingredients in the smallest unit you cook with (g, ml) and leave units_per_base at 1.
    """
    variant = models.OneToOneField(ProductVariant, on_delete=models.PROTECT, related_name="ingredient")
    recipe_unit = models.CharField(max_length=15, blank=True,
                                   help_text="Unit recipes use. Blank = the stock unit.")
    units_per_base = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("1"),
        help_text="Recipe units in one stock unit (stock in kg, recipes in g -> 1000).")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["variant__product__name"]
        constraints = [models.CheckConstraint(condition=Q(units_per_base__gt=0), name="ingredient_units_gt_0")]

    def __str__(self):
        return self.name

    @property
    def name(self):
        return self.variant.product.name

    @property
    def stock_unit(self):
        return self.variant.unit_label

    @property
    def unit_label(self):
        return self.recipe_unit or self.stock_unit

    @property
    def cost_per_recipe_unit(self):
        return Decimal(self.variant.cost_price) / Decimal(self.units_per_base)


class Recipe(TenantOwnedModel):
    menu_item = models.OneToOneField(MenuItem, on_delete=models.CASCADE, related_name="recipe")
    yield_portions = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("1"),
        help_text="How many portions the quantities below make (a pot of stew for 10 -> 10).")
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(yield_portions__gt=0), name="recipe_yield_gt_0")]

    def __str__(self):
        return f"Recipe: {self.menu_item.name}"


class RecipeLine(TenantOwnedModel):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="lines")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, related_name="recipe_lines")
    quantity = models.DecimalField(max_digits=12, decimal_places=3, help_text="In the ingredient's recipe unit.")
    waste_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Trim / cooking loss. 10 means 10% more must be bought than ends up on the plate.")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["recipe", "ingredient"], name="uniq_ingredient_per_recipe"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="recipeline_qty_gt_0"),
        ]

    def __str__(self):
        return f"{self.quantity} {self.ingredient.unit_label} {self.ingredient.name}"


class Modifier(TenantOwnedModel):
    """A customization: 'No onions', 'Extra cheese (+50)', 'Well done'."""
    class Kind(models.TextChoices):
        EXTRA = "EXTRA", "Extra (adds to the price / uses more stock)"
        REMOVE = "REMOVE", "Removal (e.g. no onions)"
        CHOICE = "CHOICE", "Preference (well done, no ice ...)"

    name = models.CharField(max_length=60)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.CHOICE)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    ingredient = models.ForeignKey(
        Ingredient, null=True, blank=True, on_delete=models.SET_NULL, related_name="modifiers",
        help_text="EXTRA: this ingredient is also used. REMOVE: this ingredient is NOT used.")
    ingredient_qty = models.DecimalField(
        max_digits=12, decimal_places=3, default=0, help_text="Recipe units of that ingredient (EXTRA only).")
    menu_items = models.ManyToManyField(MenuItem, blank=True, related_name="modifiers",
                                        help_text="Leave empty to offer it on every item.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["kind", "name"]
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="uniq_modifier_name_per_tenant")]

    def __str__(self):
        return self.name


# =====================================================================================
# Orders (checks), lines, kitchen tickets
# =====================================================================================
class DeliveryZone(TenantOwnedModel):
    name = models.CharField(max_length=60)
    fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="uniq_delivery_zone")]

    def __str__(self):
        return f"{self.name} (KSH {self.fee})"


class DeliveryRider(TenantOwnedModel):
    name = models.CharField(max_length=80)
    phone = models.CharField(max_length=20, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class RestaurantOrder(TenantOwnedModel):
    class Type(models.TextChoices):
        DINE_IN = "DINE_IN", "Dine-in"
        TAKEAWAY = "TAKEAWAY", "Takeaway"
        DELIVERY = "DELIVERY", "Delivery"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        PAID = "PAID", "Paid"
        VOID = "VOID", "Cancelled"

    class Source(models.TextChoices):
        POS = "POS", "In restaurant"
        PHONE = "PHONE", "Phone"
        ONLINE = "ONLINE", "Online"

    class Handover(models.TextChoices):
        PENDING = "PENDING", "Being prepared"
        READY = "READY", "Ready"
        ASSIGNED = "ASSIGNED", "Rider assigned"
        OUT = "OUT", "Out for delivery"
        COMPLETED = "COMPLETED", "Delivered / collected"
        FAILED = "FAILED", "Delivery failed"

    class DiscountType(models.TextChoices):
        NONE = "NONE", "None"
        PERCENT = "PERCENT", "Percent"
        AMOUNT = "AMOUNT", "Fixed amount"

    number = models.CharField(max_length=20)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="restaurant_orders")
    order_type = models.CharField(max_length=10, choices=Type.choices, default=Type.DINE_IN)
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN, db_index=True)
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.POS)
    external_ref = models.CharField(max_length=80, blank=True, help_text="Id on the online ordering platform.")
    needs_acceptance = models.BooleanField(default=False, help_text="Online order waiting for staff to accept it.")

    table = models.ForeignKey(Table, null=True, blank=True, on_delete=models.SET_NULL, related_name="orders")
    guests = models.PositiveSmallIntegerField(default=1)
    waiter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="served_orders")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                   related_name="+")
    split_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="split_children")
    notes = models.CharField(max_length=300, blank=True)

    # takeaway / delivery
    customer_name = models.CharField(max_length=80, blank=True)
    customer_phone = models.CharField(max_length=20, blank=True)
    delivery_address = models.CharField(max_length=300, blank=True)
    delivery_notes = models.CharField(max_length=300, blank=True)
    delivery_zone = models.ForeignKey(DeliveryZone, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    rider = models.ForeignKey(DeliveryRider, null=True, blank=True, on_delete=models.SET_NULL, related_name="orders")
    handover = models.CharField(max_length=10, choices=Handover.choices, default=Handover.PENDING)
    handover_at = models.DateTimeField(null=True, blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)

    # money (kept in step with the lines by recalculate())
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_type = models.CharField(max_length=7, choices=DiscountType.choices, default=DiscountType.NONE)
    discount_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_reason = models.CharField(max_length=120, blank=True)
    discount_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    service_charge_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    service_charge = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    delivery_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    sales_order = models.OneToOneField("sales.Order", null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="restaurant_order")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="uniq_restaurant_order_number"),
            models.UniqueConstraint(fields=["tenant", "external_ref"], condition=~Q(external_ref=""),
                                    name="uniq_restaurant_order_external_ref"),
        ]
        indexes = [models.Index(fields=["tenant", "branch", "status"])]

    def __str__(self):
        return self.number

    # ---- derived money
    @property
    def amount_paid(self):
        return sum((p.amount for p in self.payments.all() if not p.is_void), ZERO)

    @property
    def balance(self):
        return money(self.total - self.amount_paid)

    @property
    def active_lines(self):
        return [l for l in self.lines.all() if l.status != OrderLine.Status.VOID]

    @property
    def draft_lines(self):
        return [l for l in self.active_lines if l.status == OrderLine.Status.DRAFT]

    @property
    def place(self):
        if self.order_type == self.Type.DINE_IN:
            return self.table.label if self.table_id else "No table"
        return self.get_order_type_display()


class OrderLine(TenantOwnedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Not sent"
        SENT = "SENT", "Sent to kitchen"
        PREPARING = "PREPARING", "Preparing"
        READY = "READY", "Ready"
        SERVED = "SERVED", "Served"
        VOID = "VOID", "Cancelled"

    order = models.ForeignKey(RestaurantOrder, on_delete=models.CASCADE, related_name="lines")
    menu_item = models.ForeignKey(MenuItem, on_delete=models.PROTECT, related_name="order_lines")
    name = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField(default=1)
    base_price = models.DecimalField(max_digits=12, decimal_places=2)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Base price plus paid extras.")
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)
    station = models.ForeignKey(Station, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    ticket = models.ForeignKey("KitchenTicket", null=True, blank=True, on_delete=models.SET_NULL, related_name="lines")
    sent_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    served_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=120, blank=True)
    stock_deducted = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="orderline_qty_gt_0")]

    def __str__(self):
        return f"{self.quantity} x {self.name}"

    @property
    def line_total(self):
        return money(self.unit_price * self.quantity)

    @property
    def extras_text(self):
        return [m.name for m in self.modifiers.all()]


class LineModifier(TenantOwnedModel):
    line = models.ForeignKey(OrderLine, on_delete=models.CASCADE, related_name="modifiers")
    modifier = models.ForeignKey(Modifier, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    name = models.CharField(max_length=60)
    kind = models.CharField(max_length=8, choices=Modifier.Kind.choices)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    ingredient = models.ForeignKey(Ingredient, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    ingredient_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0)

    def __str__(self):
        return self.name


class KitchenTicket(TenantOwnedModel):
    """The slip a station sees: every item of one order that is made at that station and was sent together."""
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PREPARING = "PREPARING", "Preparing"
        READY = "READY", "Ready"
        SERVED = "SERVED", "Served"
        CANCELLED = "CANCELLED", "Cancelled"

    order = models.ForeignKey(RestaurantOrder, on_delete=models.CASCADE, related_name="tickets")
    station = models.ForeignKey(Station, on_delete=models.PROTECT, related_name="tickets")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="+")
    round = models.PositiveSmallIntegerField(default=1, help_text="1st, 2nd ... time items were sent for this order.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    fired_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    served_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    fired_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    worked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")

    class Meta:
        ordering = ["fired_at", "id"]
        indexes = [models.Index(fields=["tenant", "branch", "status"])]

    def __str__(self):
        return f"{self.order.number} / {self.station.name} #{self.round}"

    @property
    def prep_seconds(self):
        """Time from the order reaching the kitchen until it was ready (None while still cooking)."""
        if self.ready_at:
            return int((self.ready_at - self.fired_at).total_seconds())
        return None


# =====================================================================================
# Stock use, waste
# =====================================================================================
class StockUse(TenantOwnedModel):
    """
    One row per stock variant taken out for an order line. It makes deduction exactly-once and lets a
    cancelled line put the very same quantities back.  `quantity` is in the stock variant's own unit.
    """
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="+")
    line = models.ForeignKey(OrderLine, on_delete=models.CASCADE, related_name="stock_uses")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    reversed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["line", "variant"], name="uniq_stockuse_per_line_variant")]


class WasteRecord(TenantOwnedModel):
    class Reason(models.TextChoices):
        SPOILAGE = "SPOILAGE", "Spoiled / went off"
        EXPIRED = "EXPIRED", "Expired"
        PREP = "PREP", "Preparation waste (trimmings, spills)"
        BURNT = "BURNT", "Burnt / cooked wrong"
        RETURNED = "RETURNED", "Returned by a guest"
        STAFF_MEAL = "STAFF_MEAL", "Staff meal"
        OTHER = "OTHER", "Other"

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="+")
    ingredient = models.ForeignKey(Ingredient, null=True, blank=True, on_delete=models.PROTECT, related_name="waste")
    menu_item = models.ForeignKey(MenuItem, null=True, blank=True, on_delete=models.PROTECT, related_name="waste")
    quantity = models.DecimalField(
        max_digits=12, decimal_places=3,
        help_text="Ingredient: in its recipe unit. Dish: number of portions.")
    reason = models.CharField(max_length=12, choices=Reason.choices, default=Reason.PREP)
    notes = models.CharField(max_length=200, blank=True)
    cost = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Value of what was lost.")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(
            condition=Q(ingredient__isnull=False) ^ Q(menu_item__isnull=False), name="waste_one_subject")]

    @property
    def subject(self):
        return self.ingredient or self.menu_item


# =====================================================================================
# Payments and shifts
# =====================================================================================
class CashierShift(TenantOwnedModel):
    """
    One cashier's stretch at the till.  The branch cash drawer is still ONE shared pool (see
    sales.CashRegisterSession); a shift answers a different question: did THIS cashier hand in what
    they collected?  Expected amounts come from the payments they took; counted amounts are typed in
    when they close.
    """
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="+")
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="shifts")
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN)
    opened_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)
    opening_float = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    expected_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    expected_mpesa = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    expected_card = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    counted_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    counted_mpesa = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    counted_card = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    variance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                   help_text="Counted minus expected, all methods together.")
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-opened_at"]
        constraints = [models.UniqueConstraint(fields=["tenant", "cashier"], condition=Q(status="OPEN"),
                                               name="one_open_shift_per_cashier")]

    def __str__(self):
        return f"Shift {self.pk} - {self.cashier}"


class BillPayment(TenantOwnedModel):
    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        MPESA = "MPESA", "M-Pesa"
        CARD = "CARD", "Card"
        ONLINE = "ONLINE", "Paid online"

    order = models.ForeignKey(RestaurantOrder, on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=8, choices=Method.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="The part of the bill this payment settles.")
    tendered = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                   help_text="Cash handed over (amount + change).")
    change_given = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reference = models.CharField(max_length=60, blank=True, help_text="M-Pesa code, card slip number ...")
    payer = models.CharField(max_length=60, blank=True, help_text="Who paid, for split bills (Guest 2, Mary).")
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    shift = models.ForeignKey(CashierShift, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments")
    created_at = models.DateTimeField(default=timezone.now)
    is_void = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="billpayment_amount_gt_0")]

    def __str__(self):
        return f"{self.get_method_display()} {self.amount}"
