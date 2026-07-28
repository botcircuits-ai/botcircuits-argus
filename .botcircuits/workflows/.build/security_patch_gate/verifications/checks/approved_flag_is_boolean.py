import sys, json

def main():
    data = json.load(sys.stdin)
    slots = data.get("slots", {}) or {}
    value = slots.get("approved_for_auto_patch", None)
    passed = isinstance(value, bool)
    detail = f"approved_for_auto_patch is boolean ({value})." if passed else f"approved_for_auto_patch is missing or not a boolean (got {value!r} of type {type(value).__name__})."
    print(json.dumps({"passed": passed, "detail": detail}))

main()
