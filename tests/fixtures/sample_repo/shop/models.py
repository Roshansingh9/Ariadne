from .utils import sum_items


class Base:
    """Persistence base class."""

    def save(self):
        self.validate()
        return True

    def validate(self):
        return None


class Order(Base):
    """An order with line items."""

    def __init__(self, items):
        self.items = items

    def total(self):
        return sum_items(self.items)

    def validate(self):
        super().validate()
        if not self.items:
            raise ValueError("empty order")
