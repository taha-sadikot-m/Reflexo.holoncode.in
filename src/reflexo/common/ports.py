"""Ports registry (POC_01 §4.2, specs_v2 D-090).

A port is a `typing.Protocol`. The owning module provides an implementation from its `api.register(registry)`;
consumers call `registry.require(PortType)`. Missing required ports abort startup (`Registry.verify`).
"""

from __future__ import annotations

from typing import Any, TypeVar, cast

T = TypeVar("T")


class PortMissing(RuntimeError):
    pass


class Registry:
    def __init__(self) -> None:
        self._impls: dict[type[Any], Any] = {}
        self._required: set[type[Any]] = set()

    def provide(self, port: type[T], impl: T) -> None:
        if port in self._impls:
            raise RuntimeError(f"port {port.__name__} already provided")
        self._impls[port] = impl

    def require(self, port: type[T]) -> T:
        try:
            return cast(T, self._impls[port])
        except KeyError as exc:
            raise PortMissing(f"no provider for port {port.__name__}") from exc

    def optional(self, port: type[T]) -> T | None:
        return cast(T | None, self._impls.get(port))

    def declare_required(self, *ports: type[Any]) -> None:
        self._required.update(ports)

    def verify(self) -> None:
        missing = [p.__name__ for p in self._required if p not in self._impls]
        if missing:
            raise PortMissing(f"required ports without provider: {', '.join(sorted(missing))}")


registry = Registry()


def reset_registry() -> Registry:
    """Fresh registry (tests, process bootstrap)."""
    global registry
    registry = Registry()
    return registry
