"""What ``featherweb run tests.sample_app.entrypoint:app`` loads."""

from featherweb import App

from .controllers.users import ScannedUserController

app = App(controllers=[ScannedUserController])
alias = app
