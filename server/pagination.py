from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

class StandardResultsSetPagination(PageNumberPagination):
    page_size_query_param = "page_size"
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        if hasattr(queryset, "ordered") and not queryset.ordered:
            if view and getattr(view, "ordering", None):
                queryset = queryset.order_by(*view.ordering)
            elif hasattr(queryset, "model"):
                meta = getattr(queryset.model, "_meta", None)
                default = getattr(meta, "ordering", None) or ("pk",)
                queryset = queryset.order_by(*default)
        return super().paginate_queryset(queryset, request, view)

    def get_paginated_response(self, data):
        return Response({
            "count": self.page.paginator.count,
            "next": self.get_next_link(),
            "previous": self.get_previous_link(),
            "results": data,
        })
