from fastapi import FastAPI

from backend.services.orders import cancel_order, place_order

app = FastAPI()


@app.post("/api/orders")
def create_order(payload: dict):
    return place_order(payload)


@app.delete("/api/orders/{order_id}")
def delete_order(order_id: int):
    return cancel_order(order_id)


@app.get("/api/health")
def health():
    return {"ok": True}
