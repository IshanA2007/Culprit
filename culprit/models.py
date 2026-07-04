"""SQLAlchemy declarative base.

Task 1 ships only the ``Base`` so Alembic's ``env.py`` can import
``Base.metadata`` (empty for now). Task 2 adds the five tables
(``Deploy``, ``Signal``, ``Incident``, ``Evidence``, ``Job``).
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base shared by every Culprit ORM model."""
