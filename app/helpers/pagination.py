MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def clamp_limit(limit: int) -> int:
    if limit < 1:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


def paginated_response(data: list, total: int, limit: int, offset: int) -> dict:
    return {
        "data": data,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
