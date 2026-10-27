# py-fastapi — minimal in-memory CRUD API (FastAPI framework)
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="py-fastapi-crud")

items = {}
_next = {"id": 1}


class ItemIn(BaseModel):
    name: str = "unnamed"


@app.get("/")
def root():
    return {"app": "py-fastapi", "framework": "fastapi", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/items")
def list_items():
    return list(items.values())


@app.post("/items", status_code=201)
def create_item(body: ItemIn):
    item = {"id": _next["id"], "name": body.name}
    items[_next["id"]] = item
    _next["id"] += 1
    return item


@app.get("/items/{item_id}")
def get_item(item_id: int):
    if item_id not in items:
        raise HTTPException(status_code=404, detail="not found")
    return items[item_id]


@app.put("/items/{item_id}")
def update_item(item_id: int, body: ItemIn):
    if item_id not in items:
        raise HTTPException(status_code=404, detail="not found")
    items[item_id]["name"] = body.name
    return items[item_id]


@app.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int):
    if items.pop(item_id, None) is None:
        raise HTTPException(status_code=404, detail="not found")
    return None
