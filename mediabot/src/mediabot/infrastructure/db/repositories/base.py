"""Generic repository implementing the storage-agnostic CRUD contract.

Architecture note
-----------------
The Repository Pattern isolates the application layer from SQLAlchemy: services
speak in terms of ``get``/``add``/``list`` and never build queries.  The generic
base removes the boilerplate; concrete repositories add the domain-specific
queries that actually deserve a name.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mediabot.infrastructure.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """CRUD operations shared by every repository."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    async def get(self, entity_id: int) -> ModelT | None:
        return await self.session.get(self.model, entity_id)

    async def get_by(self, **filters: Any) -> ModelT | None:
        stmt = select(self.model).filter_by(**filters).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        order_by: Any | None = None,
        **filters: Any,
    ) -> Sequence[ModelT]:
        stmt: Select[tuple[ModelT]] = select(self.model).filter_by(**filters)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        stmt = stmt.limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def count(self, **filters: Any) -> int:
        stmt = select(func.count()).select_from(self.model).filter_by(**filters)
        return int((await self.session.execute(stmt)).scalar_one())

    async def exists(self, **filters: Any) -> bool:
        stmt = select(self.model.id).filter_by(**filters).limit(1)  # type: ignore[attr-defined]
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #
    def add(self, entity: ModelT) -> ModelT:
        """Stage a new entity; the UoW decides when it is flushed/committed."""
        self.session.add(entity)
        return entity

    async def create(self, **values: Any) -> ModelT:
        entity = self.model(**values)
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def update_by_id(self, entity_id: int, **values: Any) -> int:
        stmt = (
            update(self.model)
            .where(self.model.id == entity_id)  # type: ignore[attr-defined]
            .values(**values)
        )
        result = await self.session.execute(stmt)
        return int(result.rowcount or 0)

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)

    async def delete_by_id(self, entity_id: int) -> int:
        stmt = delete(self.model).where(self.model.id == entity_id)  # type: ignore[attr-defined]
        result = await self.session.execute(stmt)
        return int(result.rowcount or 0)

    async def flush(self) -> None:
        await self.session.flush()
