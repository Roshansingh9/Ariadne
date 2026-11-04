import json
import os.path


def sum_items(items):
    return fmt(sum(i["price"] for i in items))


def fmt(value):
    return round(value, 2)


def dump(obj):
    os.path.join("a", "b")
    return json.dumps(obj)
