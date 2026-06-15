from typing import Sequence, TypeVar

from app.schemas import Paginated

T = TypeVar("T")


def paginate(items: Sequence[T], page: int = 1, page_size: int = 20, total: int | None = None) -> Paginated[T]:
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    total_count = total if total is not None else len(items)
    start = (page - 1) * page_size
    end = start + page_size
    page_items = list(items[start:end])
    return Paginated(
        items=page_items,
        page=page,
        pageSize=page_size,
        total=total_count,
        hasMore=end < total_count,
    )
