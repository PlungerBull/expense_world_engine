"""The pagination envelope, as a typed response model.

``helpers/pagination.paginated_response`` builds the dict; this model is what
routes declare as ``response_model=Paginated[ItemModel]`` so the envelope is
enforced and documented (bug 10.1). One generic, nine list routes — not nine
concrete envelope classes.
"""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Paginated(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
