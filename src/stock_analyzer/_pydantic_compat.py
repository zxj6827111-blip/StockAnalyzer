"""Typed compatibility layer for pydantic under mypy follow-imports=skip."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

    _Decorator = TypeVar("_Decorator", bound=Callable[..., Any])

    class ConfigDict(dict[str, object]):
        def __init__(self, **kwargs: object) -> None: ...

    class BaseModel:
        model_config: ConfigDict

        def __init__(self, **data: object) -> None: ...

        @classmethod
        def model_validate(cls: type[Self], obj: object, **kwargs: object) -> Self: ...

        @classmethod
        def model_validate_json(
            cls: type[Self],
            json_data: str | bytes | bytearray,
            **kwargs: object,
        ) -> Self: ...

        def model_copy(
            self: Self,
            *,
            update: dict[str, object] | None = None,
            **kwargs: object,
        ) -> Self: ...

        def model_dump(self, *, mode: str = "python", **kwargs: object) -> dict[str, object]: ...

        def model_dump_json(self, *, indent: int | None = None, **kwargs: object) -> str: ...

    def Field(
        default: object = ...,
        *,
        default_factory: Callable[[], object] | None = None,
        alias: str | None = None,
        **kwargs: object,
    ) -> Any: ...

    def field_validator(
        *fields: str,
        mode: str = "after",
        check_fields: bool | None = None,
        **kwargs: object,
    ) -> Callable[[_Decorator], _Decorator]: ...
else:
    from pydantic import BaseModel as BaseModel
    from pydantic import ConfigDict as ConfigDict
    from pydantic import Field as Field
    from pydantic import field_validator as field_validator
