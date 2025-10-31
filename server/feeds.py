from django.http import JsonResponse
from .models import Product

def merchant_products_feed(request):
    items = []
    for p in Product.objects.all():
        items.append({
            "id": str(p.id),
            "title": p.title_en,
            "description": p.short_description or p.title_en,
            "link": f"https://mindmines.eu/decorative-products/{p.id}/",
            "image_link": p.images.first().image.url if hasattr(p, "images") and p.images.first() else "",
            "availability": "in stock" if getattr(p, "in_stock", True) else "out of stock",
            "price": f"{p.price:.2f} EUR",
            "brand": "MindMines",
            "condition": "new",
          # add gtin/mpn if you have them
        })
    return JsonResponse({"items": items})
