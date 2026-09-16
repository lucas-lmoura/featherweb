"""A controller that scan() has to find."""

from featherweb import Get, Route


@Route("/scanned/users")
class ScannedUserController:
    @Get
    async def index(self) -> list[str]:
        return ["ada"]
