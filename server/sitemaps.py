# server/sitemaps.py
from typing import Any, List, Union, cast
from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from django.db.models import Model, QuerySet
from .models import Product


class ProductSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def items(self) -> QuerySet[Product]:
        return Product.objects.all().only("pk", "created_at")

    def location(self, obj: Model) -> str:
        p = cast(Product, obj)
        return reverse("product-detail", kwargs={"pk": p.pk})

    def lastmod(self, obj: Model):
        p = cast(Product, obj)
        return getattr(p, "updated_at", None) or getattr(p, "created_at", None)


class CategorySitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.6

    def items(self) -> Union[QuerySet, List[Model]]:
        # Import Category only at runtime; if it doesn't exist, return empty list
        try:
            from .models import Category  # type: ignore
        except Exception:
            return []
        return cast(QuerySet, Category.objects.all())

    def location(self, obj: Model) -> str:
        slug = getattr(obj, "slug", None)
        return f"/category/{slug}/" if slug else "/category/"

    def lastmod(self, obj: Model):
        return getattr(obj, "updated_at", None) or getattr(obj, "created_at", None)


class StaticSitemap(Sitemap):
    priority = 0.5
    changefreq = "monthly"

    def items(self) -> List[str]:
        return ["home", "contact"]

    def location(self, obj: Model) -> str:
        # items() yields strings; base signature expects Model -> accept Model and cast
        name = cast(str, obj)
        return reverse(name)
