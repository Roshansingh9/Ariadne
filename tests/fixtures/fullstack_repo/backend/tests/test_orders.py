from fastapi.testclient import TestClient

from backend.app import app
from backend.services.orders import place_order

client = TestClient(app)


def test_place_order_total():
    assert place_order({"items": [{"price": 1.5}]})["total"] == 150


def test_create_order_endpoint():
    response = client.post("/api/orders", json={"items": [{"price": 2}]})
    assert response.status_code == 200
