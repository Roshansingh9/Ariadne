import requests

from . import utils
from .models import Order


class OrderService:
    def place(self, data):
        order = Order(data)
        order.save()
        utils.dump(data)
        self.notify(order)
        requests.post("https://example.invalid/orders", json=data)
        return order

    def notify(self, order):
        log_event(order)
