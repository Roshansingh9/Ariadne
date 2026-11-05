import requests

from backend.models import Order
from backend.utils.money import to_cents


def place_order(payload):
    order = Order(payload["items"])
    order.validate()
    total = to_cents(order.total())
    notify_warehouse(order)
    return {"total": total}


def cancel_order(order_id):
    return reconcile(order_id, 0)


def reconcile(order_id, attempt):
    if attempt < 3:
        return retry_reconcile(order_id, attempt + 1)
    return order_id


def retry_reconcile(order_id, attempt):
    return reconcile(order_id, attempt)


def notify_warehouse(order):
    requests.post("https://warehouse.example.invalid/notify", json={"n": len(order.items)})
