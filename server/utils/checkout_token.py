# server/utils/checkout_token.py
import json, base64

def pack_checkout_payload(payload: dict) -> str:
    # Keep tiny: only the fields you need to build an Order
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    b = raw.encode("utf-8")
    return base64.urlsafe_b64encode(b).decode("ascii")

def unpack_checkout_payload(token: str) -> dict:
    b = base64.urlsafe_b64decode(token.encode("ascii"))
    return json.loads(b.decode("utf-8"))
