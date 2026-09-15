# Plano — `leve`: micro framework web async para Python

> Nome `leve` é provisório (verificar disponibilidade no PyPI antes da Fase 7).

## 1. Decisões de base

| Tema | Decisão |
|---|---|
| I/O | `asyncio` + servidor HTTP/1.1 próprio (`asyncio.Protocol`) |
| Interface interna | **ASGI 3** — o app roda no servidor próprio (`app.run()`) **e** em uvicorn/hypercorn/granian; o servidor roda qualquer app ASGI |
| Dependências | Runtime só com stdlib. Extras opcionais detectados por `try/import`: `uvloop` (Linux/macOS) / `winloop` (Windows), `orjson` |
| Python | 3.12+ (CI em 3.12, 3.13, 3.14) |
| MVP v0.1 | Núcleo HTTP · estáticos + streaming + multipart · WebSocket · injeção/validação por type hints |

## 2. O que "leve" significa (metas mensuráveis)

- Zero dependências obrigatórias.
- Import do pacote < 15 ms (`python -X importtime -c "import leve"`); módulos pesados (multipart, websocket, staticfiles) importados sob demanda.
- Núcleo com ~3.000 linhas ou menos.
- Memória ociosa < 20 MB por processo.
- Hello world / JSON com throughput na faixa de Starlette+uvicorn (benchmark reprodutível em `benchmarks/`).

## 3. Arquitetura

```
socket ─► HttpProtocol (asyncio.Protocol)
             │  parser HTTP/1.1 incremental · keep-alive · backpressure · timeouts
             ▼
          scope / receive / send   (ASGI 3)
             ▼
          App.__call__ ─► middlewares ─► Router ─► handler (parâmetros por type hints)
                                                        ▼
                                                    Response ─► send()
```

Princípios:
- **Custo no registro, não na requisição**: assinatura do handler, conversores de rota e extratores de parâmetros são "compilados" uma vez em `@app.get(...)`.
- **Tudo preguiçoso**: body, JSON, form, cookies e query só são parseados quando acessados.
- **Sem mágica global**: nada de `request` global/contextvar implícito; o handler recebe o que declara.

## 4. Estrutura do pacote

```
leve/
  __init__.py        API pública (App, Request, Response, run, ...)
  _compat.py         detecção de uvloop/winloop/orjson; json_dumps/json_loads
  server/
    parser.py        parser HTTP/1.1 incremental (request line, headers, Content-Length, chunked)
    protocol.py      HttpProtocol: keep-alive, pipelining, flow control, Expect: 100-continue
    ws_protocol.py   upgrade + frames RFC 6455 (masking, fragmentação, ping/pong, close)
    runner.py        run(): event loop, sinais, graceful shutdown, TLS (ssl.SSLContext), workers
  app.py             App: ASGI callable, lifespan (startup/shutdown), mount(), handlers de erro
  routing.py         Router: rotas estáticas em dict O(1) + árvore de segmentos p/ dinâmicas
  request.py         Request: method, path, headers, query, cookies, body()/json()/form()/stream()
  response.py        Response, JSONResponse, HTMLResponse, PlainText, Redirect, Streaming, File
  params.py          DI por type hints + validação leve
  middleware.py      middleware "cebola": async def mw(request, call_next)
  multipart.py       parser multipart/form-data em streaming (spool em disco acima de N KB)
  staticfiles.py     StaticFiles: ETag, Last-Modified, 304, Range/206, guarda contra path traversal
  websocket.py       API alto nível: accept, receive_*/send_*, iter_text/iter_json, close
  exceptions.py      HTTPException e afins
  testing.py         TestClient in-process (chama o ASGI direto, sem socket)
tests/  benchmarks/  examples/
```

## 5. API alvo

```python
from dataclasses import dataclass
from typing import Annotated
from leve import App, Request, Header, StaticFiles, WebSocket

app = App()


@dataclass
class ItemIn:
    name: str
    price: float


@app.get("/items/{item_id:int}")
async def get_item(item_id: int, q: str | None = None):  # item_id: rota; q: query
    return {"id": item_id, "q": q}  # dict -> JSON


@app.post("/items")
async def create(item: ItemIn, token: Annotated[str, Header("x-token")]):
    return 201, {"name": item.name}  # (status, body)


@app.get("/raw")
def sync_handler(request: Request):  # def síncrono -> roda em thread
    return f"olá {request.client}"  # str -> text/plain


@app.websocket("/ws")
async def echo(ws: WebSocket):
    await ws.accept()
    async for msg in ws.iter_text():
        await ws.send_text(msg)


app.mount("/static", StaticFiles("public"))

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
```

### Regras de injeção de parâmetros
1. Nome igual a um parâmetro da rota → path param (convertido pelo conversor `int`, `float`, `str`, `path`, `uuid`).
2. `Request` / `WebSocket` → o próprio objeto.
3. `dataclass`, `TypedDict` ou modelo pydantic (duck typing via `model_validate`, se o usuário tiver pydantic) → corpo JSON.
4. Tipos simples (`str`, `int`, `float`, `bool`, `Enum`, `Literal`, `X | None`, `list[X]`) → query string.
5. Explícito via `Annotated[T, Query()/Header()/Cookie()/Body()/Form()/File()]`.
6. Erro de validação → 422 com JSON descrevendo campo e motivo.

### Retornos aceitos
`Response` (como está) · `dict`/`list` → JSON · `str` → text/plain · `bytes` → octet-stream · `None` → 204 · `(status, body)` · `(status, body, headers)` · gerador async → streaming.

## 6. Pontos críticos por componente

**Parser HTTP/1.1** (maior risco de segurança)
- Incremental sobre `bytearray`; limites: request line 8 KB, headers 64 KB / 100 campos, body configurável (padrão 1 MB; streaming não bufferiza).
- Rejeitar `Content-Length` + `Transfer-Encoding` juntos, `Content-Length` duplicado/divergente, obs-fold e nomes de header inválidos (anti request smuggling) → 400.
- HTTP/1.0 e 1.1, keep-alive, `Connection: close`, pipelining sequencial, `Expect: 100-continue`.

**Protocolo / servidor**
- Backpressure: `pause_reading()` quando o buffer do body enche; `pause_writing/resume_writing` para o `send`.
- Timeouts: leitura de headers (anti slowloris), keep-alive ocioso (5 s), shutdown gracioso (espera requisições em andamento com prazo).
- `Content-Length` automático; chunked quando streaming sem tamanho; header `Date` cacheado por segundo.
- Workers: `--workers N` com `SO_REUSEPORT` no Linux; no Windows, 1 processo no MVP (sem `SO_REUSEPORT`, sem uvloop — `winloop` opcional).

**WebSocket**: handshake (`Sec-WebSocket-Accept` = base64(sha1)), exigir mascaramento do cliente, fragmentação, ping/pong automático, códigos de close, tamanho máximo de mensagem. Sem `permessage-deflate` no MVP.

**Estáticos**: `Path.resolve()` + `is_relative_to()` contra traversal; ETag (mtime+tamanho), 304, `Range` → 206, `mimetypes`; `loop.sendfile` com fallback em blocos.

**Multipart**: parser em streaming por boundary; campos pequenos em memória, arquivos em `SpooledTemporaryFile`; limites de partes e tamanho.

## 7. Ferramentas e testes

- Projeto gerenciado com `uv` (`uv init --lib`), `pyproject.toml` com build backend `hatchling`.
- Dev deps (não vão para o runtime): `pytest`, `pytest-asyncio`, `httpx` (cliente em testes com socket real), `websockets` (cliente WS), `hypothesis` (fuzz do parser), `ruff`, `pyright`, `coverage`, `uvicorn` (testes de compatibilidade).
- Camadas de teste:
  1. Unitários do parser, incluindo casos malformados e de smuggling.
  2. `TestClient` in-process para o framework.
  3. Integração com socket real contra o servidor próprio.
  4. **Compatibilidade ASGI**: mesma suíte do framework rodando sob uvicorn; servidor próprio rodando um app Starlette mínimo.
  5. (Opcional) Autobahn TestSuite para WebSocket.
- Benchmarks com `oha` (funciona no Windows): hello, JSON, path param, arquivo estático — comparando com Starlette/FastAPI sob uvicorn. Import time e memória também registrados.
- CI (GitHub Actions): Python 3.12/3.13/3.14 × Linux/Windows/macOS.

## 8. Fases

| Fase | Entrega | Pronto quando |
|---|---|---|
| 0. Setup | `git init`, `uv init --lib`, ruff/pyright/pytest, CI | `uv run pytest` passa no CI |
| 1. Servidor HTTP | `parser.py`, `protocol.py`, `runner.py` mínimos | App ASGI "cru" responde; keep-alive e pipelining testados; casos de smuggling rejeitados |
| 2. Núcleo do framework | `App`, `Router`, `Request`, `Response`, erros, middleware, lifespan, `TestClient` | Mesma suíte passa no servidor próprio e no uvicorn |
| 3. Type hints | `params.py`: extração + validação + 422 | Todas as regras da seção 5 cobertas por testes |
| 4. Streaming e arquivos | `StreamingResponse`, `FileResponse`, `StaticFiles`, multipart | Upload de 100 MB sem estourar memória; 304/206 corretos |
| 5. WebSocket | `ws_protocol.py` + `websocket.py` | Echo com cliente `websockets`; close/ping corretos |
| 6. Endurecimento e desempenho | timeouts, limites, graceful shutdown, workers, TLS, extras opcionais, profiling | Metas da seção 2 atingidas e registradas |
| 7. Release v0.1 | README, exemplos, docs da API, publicação no PyPI | `pip install leve` + exemplo do README funciona |

## 9. Fora do escopo da v0.1 (candidatos a v0.2+)
HTTP/2 e HTTP/3 · geração de OpenAPI (os type hints da Fase 3 já dão a base) · hot reload · `permessage-deflate` · sessões/auth · templates (usuário integra Jinja se quiser) · parser C opcional (`httptools`).

## 10. Riscos
- **Parser próprio e segurança** → parsing estrito, limites conservadores, testes de smuggling, fuzz com hypothesis.
- **Parser em Python puro mais lento que C** → aceitar no MVP; medir; fast path opcional depois.
- **Diferenças Windows × Linux** (Proactor loop, sem uvloop, sem `SO_REUSEPORT`) → CI em matriz desde a Fase 0.
- **Overhead dos dicts ASGI** → medir na Fase 6; só otimizar se o benchmark justificar.
