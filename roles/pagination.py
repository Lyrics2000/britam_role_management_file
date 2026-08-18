"""Pagination.

ADR-010: the browse page filters, sorts and compares entirely client-side (it
did so as a static file and users expect instant filtering). It therefore wants
the whole data set in one response. 290 rows of ~1.5 KB is ~430 KB, ~60 KB
gzipped — one request, cached. So the default page size is large and callers
can page explicitly with ?page_size=. A hard max stops a crafted request from
asking for an unbounded result set.
"""

from __future__ import annotations

from rest_framework.pagination import PageNumberPagination


class RolePagination(PageNumberPagination):
    page_size = 500
    page_size_query_param = "page_size"
    max_page_size = 2000
