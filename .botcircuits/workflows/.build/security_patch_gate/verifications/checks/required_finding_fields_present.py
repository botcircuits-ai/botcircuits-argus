import sys, json

def main():
    data = json.load(sys.stdin)
    slots = data.get("slots", {}) or {}
    required = ["finding_id", "repo", "file", "severity", "title", "description"]
    missing = [k for k in required if not isinstance(slots.get(k), str) or not slots.get(k).strip()]
    passed = len(missing) == 0
    detail = "All required finding fields present and non-empty." if passed else f"Missing or empty required slot(s): {missing}"
    print(json.dumps({"passed": passed, "detail": detail}))

main()
