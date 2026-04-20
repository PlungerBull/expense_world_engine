MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def paginated_response(items: list, total: int, limit: int, offset: int) -> dict:
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
