import json
import os

def get_config(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def set_config(path, data):
    with open(path, "w") as f:
        json.dump(data, f)
