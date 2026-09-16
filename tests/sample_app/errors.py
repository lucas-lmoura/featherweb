"""Global error handling that scan() has to find."""

from featherweb import ControllerAdvice, ExceptionHandler, Response


class Unreachable(Exception):
    """Raised by the sample controller to exercise the global handler."""


@ControllerAdvice
class ScannedErrors:
    @ExceptionHandler(Unreachable)
    async def unreachable(self, exc: Unreachable) -> Response[dict[str, str]]:
        return Response({"detail": f"scanned: {exc}"}, status=503)
