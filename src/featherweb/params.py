"""Turning a request into the arguments a handler asked for.

The work happens once, when the route is registered: the handler's signature is
read there and compiled into a small list of instructions, so serving a request
is a loop over a tuple instead of a walk over annotations.

Where each parameter comes from is inferred, in this order:

1. a name that appears in the route path is that path parameter;
2. a framework type (``Request``) is the object itself;
3. a dataclass, a ``TypedDict`` or a pydantic model is the JSON body;
4. anything else simple (``str``, ``int``, ``Enum``, ``list[X]``, ``X | None``…)
   is a query parameter;
5. ``Annotated[T, Query() / Header() / Cookie() / Body() / Form()]`` overrides
   all of the above. Each marker takes the name to look under, so ``Body()`` is
   the whole body while ``Body("count")`` is that one field of it.

Whatever does not fit is an error at startup, not a surprise per request.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar, cast

from ._introspect import (
    MISSING,
    is_body_hint,
    resolve_hints,
    unwrap_annotated,
    unwrap_optional,
)
from .exceptions import FieldError, ValidationError
from .request import Request
from .validation import Invalid, UnsupportedType, Validator, compile_validator

__all__ = [
    "Binder",
    "Body",
    "Cookie",
    "Form",
    "Header",
    "Query",
    "RouteConfigurationError",
    "compile_binder",
]


class RouteConfigurationError(TypeError):
    """A handler declares something the framework cannot provide."""


class _Marker:
    """Base of the ``Annotated`` markers that pin a parameter to a source."""

    __slots__ = ("alias",)
    location: ClassVar[str] = ""

    def __init__(self, alias: str | None = None) -> None:
        self.alias = alias

    def __repr__(self) -> str:
        name = type(self).__name__
        return f"{name}({self.alias!r})" if self.alias else f"{name}()"


class Query(_Marker):
    """Read this parameter from the query string."""

    __slots__ = ()
    location = "query"


class Header(_Marker):
    """Read this parameter from a request header."""

    __slots__ = ()
    location = "header"


class Cookie(_Marker):
    """Read this parameter from a cookie."""

    __slots__ = ()
    location = "cookie"


class Body(_Marker):
    """Read this parameter from the JSON body."""

    __slots__ = ()
    location = "body"


class Form(_Marker):
    """Read this parameter from a form-encoded body."""

    __slots__ = ()
    location = "form"


class _Instruction:
    """How to produce one argument."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    async def extract(
        self, request: Request, path_params: Mapping[str, Any], exception: BaseException | None
    ) -> Any:
        raise NotImplementedError


class _GiveRequest(_Instruction):
    __slots__ = ()

    async def extract(
        self, request: Request, path_params: Mapping[str, Any], exception: BaseException | None
    ) -> Any:
        return request


class _GiveException(_Instruction):
    __slots__ = ()

    async def extract(
        self, request: Request, path_params: Mapping[str, Any], exception: BaseException | None
    ) -> Any:
        return exception


class _FromRequest(_Instruction):
    """A value read from the path, query, headers, cookies, body or a form."""

    __slots__ = ("alias", "default", "default_factory", "location", "multiple", "validate")

    def __init__(
        self,
        name: str,
        *,
        location: str,
        alias: str,
        validate: Validator,
        multiple: bool,
        default: Any,
        default_factory: Callable[[], Any] | None,
    ) -> None:
        super().__init__(name)
        self.location = location
        self.alias = alias
        self.validate = validate
        self.multiple = multiple
        self.default = default
        self.default_factory = default_factory

    async def extract(
        self, request: Request, path_params: Mapping[str, Any], exception: BaseException | None
    ) -> Any:
        raw = await self._read(request, path_params)
        if raw is MISSING:
            return self._fallback()
        try:
            return self.validate(raw)
        except Invalid as exc:
            raise ValidationError(
                [
                    FieldError(self.location, _path(self.alias, path), message)
                    for path, message in exc.problems
                ]
            ) from None

    def _fallback(self) -> Any:
        if self.default_factory is not None:
            return self.default_factory()
        if self.default is not MISSING:
            return self.default
        raise ValidationError([FieldError(self.location, self.alias, "field required")])

    async def _read(self, request: Request, path_params: Mapping[str, Any]) -> Any:
        location = self.location
        if location == "path":
            return path_params.get(self.alias, MISSING)
        if location == "query":
            return _from_multi(request.query, self.alias, self.multiple)
        if location == "header":
            values = request.headers.getlist(self.alias)
            if not values:
                return MISSING
            return values if self.multiple else values[0]
        if location == "cookie":
            return request.cookies.get(self.alias, MISSING)
        if location == "form":
            return _from_multi(await request.form(), self.alias, self.multiple)
        return await self._from_json_field(request)

    async def _from_json_field(self, request: Request) -> Any:
        """One named field of the JSON body, for ``Annotated[T, Body("name")]``."""
        payload = await _read_json(request)
        if payload is MISSING:
            return MISSING
        if not isinstance(payload, Mapping):
            raise ValidationError([FieldError("body", self.alias, "expected an object")])
        return cast(Mapping[str, Any], payload).get(self.alias, MISSING)


class _WholeBody(_Instruction):
    """The JSON body, validated as a whole."""

    __slots__ = ("default", "default_factory", "validate")

    def __init__(
        self,
        name: str,
        *,
        validate: Validator,
        default: Any,
        default_factory: Callable[[], Any] | None,
    ) -> None:
        super().__init__(name)
        self.validate = validate
        self.default = default
        self.default_factory = default_factory

    async def extract(
        self, request: Request, path_params: Mapping[str, Any], exception: BaseException | None
    ) -> Any:
        payload = await _read_json(request)
        if payload is MISSING:
            if self.default_factory is not None:
                return self.default_factory()
            if self.default is not MISSING:
                return self.default
            raise ValidationError([FieldError("body", "", "a request body is required")])
        try:
            return self.validate(payload)
        except Invalid as exc:
            raise ValidationError(
                [FieldError("body", ".".join(path), message) for path, message in exc.problems]
            ) from None


class Binder:
    """Compiled recipe for a handler's arguments."""

    __slots__ = ("_instructions",)

    def __init__(self, instructions: tuple[_Instruction, ...]) -> None:
        self._instructions = instructions

    @property
    def empty(self) -> bool:
        return not self._instructions

    async def build(
        self,
        request: Request,
        path_params: Mapping[str, Any],
        exception: BaseException | None = None,
    ) -> dict[str, Any]:
        """Collect every argument, reporting all the invalid ones at once."""
        if not self._instructions:
            return {}
        arguments: dict[str, Any] = {}
        errors: list[FieldError] = []
        for instruction in self._instructions:
            try:
                arguments[instruction.name] = await instruction.extract(
                    request, path_params, exception
                )
            except ValidationError as exc:
                errors.extend(exc.errors)
        if errors:
            raise ValidationError(errors)
        return arguments

    def __repr__(self) -> str:
        bound = ", ".join(instruction.name for instruction in self._instructions)
        return f"Binder({bound})"


def compile_binder(
    handler: Callable[..., Any],
    *,
    path_params: tuple[str, ...] = (),
    exception_type: type[BaseException] | None = None,
    where: str = "",
) -> Binder:
    """Work out where each parameter of ``handler`` comes from.

    ``handler`` is the bound method, so ``self`` is already out of the picture.
    """
    import inspect  # deferred: registration only, never on the request path

    where = where or getattr(handler, "__qualname__", repr(handler))
    hints = resolve_hints(handler)
    instructions: list[_Instruction] = []
    wants_exception = exception_type is not None
    for name, parameter in inspect.signature(handler).parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        raw = hints.get(name, parameter.annotation)
        annotation = None if raw is parameter.empty else raw
        hint, markers = unwrap_annotated(annotation)

        if hint is Request:
            instructions.append(_GiveRequest(name))
            continue
        marker = next((item for item in markers if isinstance(item, _Marker)), None)
        if wants_exception and marker is None and name not in path_params:
            instructions.append(_GiveException(name))
            wants_exception = False
            continue
        if annotation is None:
            if parameter.default is not parameter.empty:
                raise RouteConfigurationError(
                    f"{where}: parameter {name!r} needs a type annotation to be read "
                    f"from the request."
                )
            raise RouteConfigurationError(
                f"{where}: parameter {name!r} has no annotation and no default."
            )
        instructions.append(
            _instruction_for(name, hint, marker, parameter, path_params, where=where)
        )
    return Binder(tuple(instructions))


def _instruction_for(
    name: str,
    hint: Any,
    marker: _Marker | None,
    parameter: Any,
    path_params: tuple[str, ...],
    *,
    where: str,
) -> _Instruction:
    inner, optional = unwrap_optional(hint)
    default: Any = MISSING if parameter.default is parameter.empty else parameter.default
    if default is MISSING and optional:
        default = None
    default, default_factory = _shared_safe(default)

    if marker is not None:
        location = marker.location
        alias = marker.alias or name
    elif name in path_params:
        location, alias = "path", name
    elif is_body_hint(inner):
        location, alias = "body", name
    else:
        location, alias = "query", name
    if location == "header":
        alias = alias.replace("_", "-").lower()

    try:
        validate = compile_validator(hint, where=where)
    except UnsupportedType as exc:
        raise RouteConfigurationError(str(exc)) from None

    # ``Body()`` takes the body as it stands; ``Body("name")`` picks that one field out of it.
    if location == "body" and (marker is None or marker.alias is None):
        return _WholeBody(name, validate=validate, default=default, default_factory=default_factory)
    if location == "body":
        return _FromRequest(
            name,
            location="body",
            alias=alias,
            validate=validate,
            multiple=False,
            default=default,
            default_factory=default_factory,
        )

    from ._introspect import collection_of

    multiple = collection_of(inner) is not None
    if location == "path" and multiple:
        raise RouteConfigurationError(f"{where}: path parameter {name!r} cannot be a collection")
    return _FromRequest(
        name,
        location=location,
        alias=alias,
        validate=validate,
        multiple=multiple,
        default=default,
        default_factory=default_factory,
    )


def _shared_safe(default: Any) -> tuple[Any, Callable[[], Any] | None]:
    """Keep a mutable default from being shared between requests.

    ``tags: list[str] = []`` would otherwise hand every request the one list the
    signature holds, so a handler that appends to it leaks into the next one.
    Containers are therefore rebuilt per request from a copy kept here.
    """
    if not isinstance(default, list | dict | set | bytearray):
        return default, None

    from copy import deepcopy  # deferred: registration only

    snapshot = deepcopy(cast(Any, default))
    return MISSING, lambda: deepcopy(snapshot)


async def _read_json(request: Request) -> Any:
    """The parsed JSON body, or ``MISSING`` when there is no body at all."""
    if not await request.body():
        return MISSING
    return await request.json()


def _from_multi(source: Any, alias: str, multiple: bool) -> Any:
    values: list[str] = source.getlist(alias)
    if not values:
        return MISSING
    return values if multiple else values[0]


def _path(alias: str, path: tuple[str, ...]) -> str:
    return ".".join((alias, *path)) if path else alias
