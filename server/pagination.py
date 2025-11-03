# server/pagination.py
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        """
        Safety net: enforce deterministic ordering before pagination when the queryset is unordered.
        """
        if hasattr(queryset, "ordered") and not queryset.ordered:
            if view and getattr(view, "ordering", None):
                queryset = queryset.order_by(*view.ordering)
            else:
                # Fall back to model Meta.ordering, or pk if none
                model = getattr(queryset, "model", None)
                default_ordering = getattr(getattr(model, "_meta", None), "ordering", None)
                queryset = queryset.order_by(*(default_ordering or ("pk",)))
        return super().paginate_queryset(queryset, request, view)

    def get_paginated_response(self, data):
        return Response({
            "count": self.page.paginator.count,
            "next": self.get_next_link(),
            "previous": self.get_previous_link(),
            "results": data,
        })
