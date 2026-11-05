from celery import Celery

from backend.services.orders import place_order

celery = Celery("shop")


@celery.task
def process_pending(payload):
    return place_order(payload)
