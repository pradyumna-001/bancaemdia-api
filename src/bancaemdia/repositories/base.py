from typing import Any

from sqlalchemy import inspect

from bancaemdia.db.models import Base


def colunas(obj: Base) -> dict[str, Any]:
    return {
        atributo.key: getattr(obj, atributo.key) for atributo in inspect(obj).mapper.column_attrs
    }
