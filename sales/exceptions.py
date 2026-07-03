class InsufficientStockError(Exception):
    def __init__(self, variant, available, requested):
        self.variant = variant
        self.available = available
        self.requested = requested
        super().__init__(
            f"Insufficient stock for {variant.sku}: "
            f"Requested {requested}, only {available} available."
        )