"""Store package — SQLite-backed CRUD (M2).

Replaces the M1 mock. CRUD functions live in `crud`; the async engine and
session factory live in `database`; ORM entities in `entities`; demo seeding
in `seed`.
"""
from __future__ import annotations

from . import crud, database, entities
