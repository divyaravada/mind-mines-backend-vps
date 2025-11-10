from typing import Any, Dict, List
from django.http import JsonResponse, HttpRequest
from .models import Product, ProductMedia

def _primary_image_url(p: Product) -> str:
    # Prefer main image; else first gallery image; else empty string
    try:
        if getattr(p, "image", None):
            return p.image.url  # FileField has .url when set
    except Exception:
        pass
    try:
        media = ProductMedia.objects.filter(product=p, media_type="image").order_by("order", "id").first()
        if media and getattr(media, "image", None):
            return media.image.url
    except Exception:
        pass
    return ""

def _product_description(p: Product) -> str:
    # Compose a short description from available fields
    parts: List[str] = []
    for f in ("short_description1_en", "short_description2_en", "short_description3_en"):
        val = getattr(p, f, "") or ""
        if val.strip():
            parts.append(val.strip())
    if not parts:
        # fall back to first paragraph if needed
        val = (getattr(p, "descriptionpara1_en", "") or "").strip()
        if val:
            parts.append(val)
    if not parts:
        parts.append(p.title_en or "")
    # Keep it reasonable for feeds
    desc = " — ".join(parts)
    return desc[:500]

def merchant_products_feed(request: HttpRequest) -> JsonResponse:
    items: List[Dict[str, Any]] = []

    # select only fields we actually use; defer heavier ones
    qs = Product.objects.all().only(
        "pk", "title_en", "price_gross", "sale_price_gross", "discount_percent",
        "quantity", "image", "category"
    )

    for p in qs:
        # link: adjust route if you use slugs/keys; here we keep id-based path
        link = f"https://mindmines.eu/decorative-products/{p.pk}/"

        # availability from stock quantity
        availability = "in stock" if int(getattr(p, "quantity", 0) or 0) > 0 else "out of stock"

        # final price (incl. VAT); your model has .final_price_gross property
        price_eur = f"{p.final_price_gross:.2f} EUR"

        items.append({
            "id": str(p.pk),
            "title": p.title_en,
            "description": _product_description(p),
            "link": link,
            "image_link": _primary_image_url(p),
            "availability": availability,
            "price": price_eur,
            "brand": "MindMines",
            "condition": "new",
            # add "gtin"/"mpn"/"google_product_category" if you have them
        })

    return JsonResponse({"items": items})
