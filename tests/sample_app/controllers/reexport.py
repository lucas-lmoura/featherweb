"""Imports a controller from elsewhere: scan() must not register it twice."""

from .users import ScannedUserController

__all__ = ["ScannedUserController"]
