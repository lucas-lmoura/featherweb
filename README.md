# featherweb

Micro framework web async para Python: servidor HTTP/1.1 próprio sobre `asyncio`,
interface ASGI e zero dependências obrigatórias.

> Em desenvolvimento. O roadmap está no [PLAN.md](PLAN.md).

## Desenvolvimento

Requer [uv](https://docs.astral.sh/uv/).

```sh
uv sync               # cria o .venv (Python 3.12) com as dependências de dev
uv run pytest         # testes
uv run ruff check     # lint
uv run ruff format    # formatação
uv run pyright        # checagem de tipos (modo strict)
```
