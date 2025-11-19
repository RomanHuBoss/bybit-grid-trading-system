from __future__ import annotations

from typing import Any, Iterable, Sequence, cast

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

__all__ = ["setup_cors"]


def setup_cors(
    app: FastAPI,
    *,
    allow_origins: Sequence[str] | None = None,
    allow_credentials: bool = True,
    allow_methods: Iterable[str] | None = None,
    allow_headers: Iterable[str] | None = None,
) -> None:
    """
    Подключить CORS-middleware к FastAPI-приложению.

    По умолчанию:
      * origins: ["*"]
      * methods: ["*"]
      * headers: ["*"]
      * credentials: True

    Более точные списки можно передать снаружи при инициализации приложения.
    """
    origins = list(allow_origins) if allow_origins is not None else ["*"]
    methods = list(allow_methods) if allow_methods is not None else ["*"]
    headers = list(allow_headers) if allow_headers is not None else ["*"]

    # cast(Any, ...) говорит тайпчекеру: "считай это подходящим типом",
    # при этом рантайм остаётся именно тем, который ожидает FastAPI/Starlette.
    app.add_middleware(
        cast(Any, CORSMiddleware),
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=methods,
        allow_headers=headers,
    )
