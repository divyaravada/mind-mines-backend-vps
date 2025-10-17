from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from .models import Product

class ProductSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return Product.objects.all()

    def location(self, obj):
        return reverse("product-detail", kwargs={"pk": obj.pk})

    def lastmod(self, obj):
        return obj.updated_at if hasattr(obj, "updated_at") else None

class CategorySitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.6
    def items(self):
        # adjust to your model; if categories aren’t a model, skip this sitemap
        from .models import Product  # or Category
        return []  # fill in real categories when available
    def location(self, obj):
        return f"/category/{obj.slug}/"

class StaticSitemap(Sitemap):
    priority = 0.5
    changefreq = "monthly"
    def items(self):
        return ["home", "contact"]  # named url patterns
    def location(self, item):
        return reverse(item)
