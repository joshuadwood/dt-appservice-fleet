# py-flask — minimal in-memory CRUD API (Flask framework)
from flask import Flask, request, jsonify

app = Flask(__name__)

items = {}
_next = {"id": 1}


@app.get("/")
def root():
    return jsonify(app="py-flask", framework="flask", status="ok")


@app.get("/health")
def health():
    return jsonify(status="healthy")


@app.get("/items")
def list_items():
    return jsonify(list(items.values()))


@app.post("/items")
def create_item():
    body = request.get_json(silent=True) or {}
    item = {"id": _next["id"], "name": body.get("name", "unnamed")}
    items[_next["id"]] = item
    _next["id"] += 1
    return jsonify(item), 201


@app.get("/items/<int:item_id>")
def get_item(item_id):
    item = items.get(item_id)
    return (jsonify(item), 200) if item else (jsonify(error="not found"), 404)


@app.put("/items/<int:item_id>")
def update_item(item_id):
    item = items.get(item_id)
    if not item:
        return jsonify(error="not found"), 404
    body = request.get_json(silent=True) or {}
    if "name" in body:
        item["name"] = body["name"]
    return jsonify(item)


@app.delete("/items/<int:item_id>")
def delete_item(item_id):
    return ("", 204) if items.pop(item_id, None) else (jsonify(error="not found"), 404)


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
