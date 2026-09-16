"""A controller whose handler raises, to reach the scanned error handler."""

from featherweb import Get, Route

from ..errors import Unreachable


@Route("/scanned")
class ScannedRootController:
    @Get("/boom")
    async def boom(self) -> str:
        raise Unreachable("gone")
