# featherweb

A lightweight async web framework for Python, inspired by Spring Boot: class-based
controllers, typed request and response handling, and its own HTTP/1.1 server built
on `asyncio`, with zero required dependencies.

> Work in progress: not ready for use yet. The roadmap (in Portuguese) is in [PLAN.md](PLAN.md).

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync               # create .venv (Python 3.12) with the dev dependencies
uv run pytest         # tests
uv run ruff check     # lint
uv run ruff format    # formatting
uv run pyright        # type checking (strict mode)
```
