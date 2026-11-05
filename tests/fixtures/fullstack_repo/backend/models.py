from pydantic import BaseModel


class Order:
    def __init__(self, items):
        self.items = items

    def total(self):
        return sum(i["price"] for i in self.items)

    def validate(self):
        if not self.items:
            raise ValueError("empty order")


class OrderIn(BaseModel):
    items: list
