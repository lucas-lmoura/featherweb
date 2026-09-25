# Plano — `featherweb`: micro framework web async para Python

> Nome `featherweb`: conferido livre no PyPI em 2026-09-16 (`/pypi/featherweb/json` responde
> 404). Continua a reservar — a publicação é o último passo da Fase 8 e está descrita em
> [RELEASING.md](RELEASING.md).

## 1. Decisões de base

| Tema | Decisão |
|---|---|
| I/O | `asyncio` + servidor HTTP/1.1 próprio (`asyncio.Protocol`) |
| Interface interna | **ASGI 3** — o app roda no servidor próprio (`app.run()`) **e** em uvicorn/hypercorn/granian; o servidor roda qualquer app ASGI |
| Estilo de API | Inspirado no Spring Boot: controllers com `@Route` + `@Get`/`@Post`/..., descobertos por scan de pacote; entrada e saída tipadas (seção 5) |
| Dependências | Runtime só com stdlib. Extras opcionais detectados por `try/import`: `uvloop` (Linux/macOS) / `winloop` (Windows), `orjson` |
| Python | 3.12+ (CI em 3.12, 3.13, 3.14) |
| Idioma | Inglês em tudo que é público (código, docstrings, mensagens do framework, README, PyPI); pt-BR neste plano e nos commits |
| MVP v0.1 | Núcleo HTTP · controllers · estáticos + streaming + multipart · WebSocket · entrada/saída por type hints · autenticação (cookie, sessão, JWT) |

## 2. Metas de leveza (mensuráveis)

Reproduzir com `python benchmarks/measure.py --all`. Os números abaixo são de
2026-09-16, com a Fase 7 pronta (Windows 10, Python 3.12, máquina de mesa).

| Meta | Alvo | Medido | |
|---|---|---|---|
| Dependências de runtime | zero | zero (`tests/test_package.py` verifica) | ✅ |
| Import do próprio pacote | < 12 ms | **9,9 ms** (com `typing` já carregado, que é o caso de qualquer aplicação) | ✅ |
| Import no interpretador limpo | registrar | **20 ms**, dos quais ~10 são o `typing` | 📌 |
| Módulos pesados fora do caminho | multipart, staticfiles, websocket, auth, logging | todos, verificado pelo benchmark | ✅ |
| Linhas de código | < 6.000 | **5.567** (8.009 no total, com 725 de docstring) | ✅ |
| Memória ociosa | < 20 MB | **16,8 MB** com a aplicação construída | ✅ |
| Throughput | faixa de Starlette+uvicorn | dentro de ~5% nas três rotas, no mesmo uvicorn | ✅ |

**As duas primeiras metas mudaram em 2026-09-16, depois de medir por quê.** O
alvo original era "import do pacote < 15 ms" e "núcleo com ~3.500 linhas". Os
dois foram escritos na Fase 0, antes de as seções 5.3 a 5.5 existirem em
detalhe, e os dois se mostraram incompatíveis com a própria seção 5:

- **Import.** Uma classe genérica PEP 695 importa o `typing` sozinha — o
  `class Response[BodyT]` herda `Generic` implicitamente, e isso basta
  (`class B[T]: pass` já coloca `typing` no `sys.modules`). Como o `Response[T]`
  da seção 5.4 é a razão de a classe ser genérica, os ~10 ms do `typing` não
  saem do caminho sem abrir mão dele. Tirar o `typing` de todo o resto do
  pacote foi tentado e medido: rendeu **0,5 ms** (20,1 → 19,6), porque o
  genérico continua puxando. E mesmo removendo os genéricos o total fica em
  **17 ms**, porque aí o pacote passa a pagar sozinho pelo `collections.abc`,
  `enum` e `re` que o `typing` carregava — só o `re` são 7 ms, puxado pelo
  `routing` para o padrão `{nome:conversor}`. Então a meta passa a medir o que
  o projeto controla: o custo do próprio pacote, com o `typing` já carregado.
  O ganho real da Fase 7 continua valendo — tirar o `logging` do caminho levou
  o total de 29 para 20 ms.
- **Linhas.** Chegar a 3.500 significa apagar cerca de 2.000 linhas testadas, e
  a conta não deixa dúvida sobre quais: multipart (334), ws_protocol (356),
  websocket (158), jwt (294), validation (287), session (124), signing (85),
  guards (82), staticfiles (49) e workers (96) somam 1.865 — exatamente o que a
  seção 1 lista como MVP da v0.1. A meta passa a ser um teto contra inchaço
  (< 6.000) em vez de um número que só se atinge removendo o produto.

Nenhuma das duas foi "resolvida" escondendo custo. Adiar o import do `typing`
para melhorar só a medição, ou expor o pacote por `__getattr__` para que
`import featherweb` não importe nada, deixariam o número bonito e o startup
real igual; não foi feito.

Throughput medido com o gerador de carga em `benchmarks/load.py`, porque não há
`oha`/`hey`/`wrk` nesta máquina; ele dirige todos os stacks pelo mesmo cliente,
então a comparação vale mesmo com os números absolutos abaixo do que uma
ferramenta nativa mostraria. A variação entre execuções chega a ±20% aqui, então
o que importa é a razão entre as colunas, não o valor. Com uvicorn nos dois
lados: featherweb 4.733/4.113/4.271 req/s contra Starlette 4.311/4.334/4.179
(plaintext/JSON/path param). O servidor próprio faz 8.649/7.729/6.912, cerca de
1,8× o uvicorn.

## 3. Arquitetura

```
socket ─► HttpProtocol (asyncio.Protocol)
             │  parser HTTP/1.1 incremental · keep-alive · backpressure · timeouts
             ▼
          scope / receive / send   (ASGI 3)
             ▼
          App.__call__ ─► middlewares ─► auth ─► Router ─► Controller.método
                                                               │  parâmetros por type hints
                                                               ▼
                                              serializador do tipo de retorno ─► send()
```

Princípios:
- **Custo no registro, não na requisição**: conversores de rota, extratores de parâmetros e serializadores de retorno são "compilados" uma vez, quando `app.scan(...)` registra os controllers.
- **Tudo preguiçoso**: body, JSON, form, cookies e query só são parseados quando acessados.
- **Sem mágica global**: nada de `request` global/contextvar implícito; o método recebe o que declara.
- **Só a requisição**: sem container de injeção de dependências. Recursos como pool de banco ficam fora do framework.

## 4. Estrutura do pacote

```
featherweb/
  __init__.py        API pública (App, Route, Get, Post, ..., Response, HTTPError, ...)
  _compat.py         detecção de uvloop/winloop/orjson; json_dumps/json_loads
  _types.py          aliases ASGI internos (scope/receive/send)
  server/
    parser.py        parser HTTP/1.1 incremental (request line, headers, Content-Length, chunked)
    protocol.py      HttpProtocol: keep-alive, pipelining, flow control, Expect: 100-continue
    ws_protocol.py   upgrade + frames RFC 6455 (masking, fragmentação, ping/pong, close)
    runner.py        run(): event loop, sinais, graceful shutdown, TLS (ssl.SSLContext), workers
  app.py             App: ASGI callable, lifespan, scan(), mount(), configuração de auth
  controllers.py     @Route, @Get/@Post/@Put/@Patch/@Delete/@Options/@Head, @Ws, descoberta por scan
  routing.py         Router: rotas estáticas em dict O(1) + árvore de segmentos p/ dinâmicas
  request.py         Request: method, path, headers, query, cookies, body()/json()/form()/stream()
  response.py        Response[T], FileResponse, StreamingResponse, RedirectResponse, cookies
  params.py          extração de parâmetros por type hints + validação leve
  serialization.py   serializadores compilados a partir do tipo de retorno
  middleware.py      @Middleware, Next; CORS e GZip prontos
  multipart.py       parser multipart/form-data em streaming (spool em disco acima de N KB)
  staticfiles.py     StaticFiles: ETag, Last-Modified, 304, Range/206, guarda contra path traversal
  websocket.py       API alto nível: accept, receive_*/send_*, iter_text/iter_json, close
  exceptions.py      HTTPError, @ExceptionHandler, @ControllerAdvice
  cli.py             comando `run` (argparse); __main__.py delega para cá
  auth/
    signing.py       assinatura HMAC para cookies e sessões
    session.py       SessionAuth e Session (sessão em cookie assinado)
    jwt.py           JWTAuth (HS256 via stdlib; RS256/ES256 com extra opcional)
    guards.py        @Authenticated, @Roles, Identity
  testing.py         TestClient in-process (chama o ASGI direto, sem socket)
tests/  benchmarks/  examples/
```

## 5. API de uso

> Decidida em 2026-09-15. Estilo inspirado no Spring Boot: controllers com decorators, retorno tipado e segurança declarativa.

### 5.1 Exemplo

```python
# myapp/controllers/users.py
from dataclasses import dataclass

from featherweb import Delete, Get, HTTPError, Post, Route


@dataclass
class UserIn:
    name: str
    email: str


@dataclass
class UserOut:
    id: int
    name: str


@Route("/users")
class UserController:
    @Get
    async def index(self, page: int = 1, size: int = 20) -> list[UserOut]:  # query
        ...

    @Get("/{id:int}")
    async def show(self, id: int) -> UserOut:  # path
        user = await find(id)
        if user is None:
            raise HTTPError(404, "user not found")
        return user  # serializado como UserOut; campos extras omitidos

    @Post(status=201)
    async def create(self, data: UserIn) -> UserOut:  # corpo JSON
        ...

    @Delete("/{id:int}")
    async def destroy(self, id: int) -> None:  # 204
        ...
```

```python
# myapp/controllers/auth.py
@Route("/auth")
class AuthController:
    @Post("/login")
    async def login(self, data: LoginIn) -> Response[TokenOut]:
        token = ...
        resp = Response(TokenOut(token=token))
        resp.set_cookie("sid", sid, httponly=True)
        return resp


# myapp/controllers/admin.py
@Route("/admin")
@Authenticated  # vale para todos os métodos
class AdminController:
    @Get("/stats")
    @Roles("admin")  # restrição extra
    async def stats(self, user: Identity) -> StatsOut: ...
```

```python
# myapp/app.py
import os

from featherweb import App, JWTAuth

app = App(auth=JWTAuth(secret=os.environ["JWT_SECRET"]))
app.scan("myapp")  # controllers, middlewares e @ControllerAdvice

if __name__ == "__main__":
    app.run(port=8000)
```

### 5.2 Rotas: controllers
- `@Route(prefixo)` na classe; verbos nos métodos: `@Get`, `@Post`, `@Put`, `@Patch`, `@Delete`, `@Options`, `@Head`. Funcionam com e sem parênteses (`@Get` usa o próprio prefixo).
- Argumentos do verbo: caminho (padrão `""`) e `status` (padrão 200; ex.: `@Post(status=201)`).
- WebSocket: método com `@Ws(caminho)` recebendo `ws: WebSocket`.
- Os decorators são classes que só anexam metadados e devolvem o próprio objeto, então o pyright mantém os tipos.
- Cada controller é instanciado uma única vez, sem argumentos, no registro.
- Registro por `app.scan("pacote")`: importa recursivamente os módulos do pacote (`pkgutil.walk_packages`) e registra as classes com `@Route` definidas neles (`cls.__module__` evita registrar duas vezes uma classe importada em outro módulo). `App(controllers=[...])` também é aceito, útil em testes.
- Rota duplicada (mesmo método + caminho) é erro no startup, não em runtime.
- Estilo único: não há `@app.get` avulso (pode ser adicionado depois sem quebrar nada).

### 5.3 Entrada: type hints com inferência
1. Nome igual a um parâmetro da rota → path param (conversores `int`, `float`, `str`, `path`, `uuid`).
2. Tipos do framework (`Request`, `WebSocket`, `Identity`, `Session`) → o próprio objeto.
3. `dataclass`, `TypedDict` ou modelo pydantic (duck typing via `model_validate`, se o usuário tiver pydantic) → corpo JSON.
4. Tipos simples (`str`, `int`, `float`, `bool`, `Enum`, `Literal`, `X | None`, `list[X]`) → query string.
5. Explícito via `Annotated[T, Query()/Header()/Cookie()/Body()/Form()/File()]`.
6. Erro de validação → 422 com JSON descrevendo campo e motivo.

Só tipos do framework são injetáveis: não há container de dependências (seção 3).

### 5.4 Saída: retorno tipado
- A anotação de retorno define a serialização, compilada no registro: `dataclass`, `TypedDict`, `list[T]`, `dict`, primitivos.
- Os campos são lidos por atributo, então qualquer objeto serve; campos fora do tipo anotado são omitidos.
- `-> None` → 204 · `-> str` → text/plain · `-> bytes` → application/octet-stream.
- Status padrão definido no decorator do verbo.
- `Response[T]` (equivalente ao `ResponseEntity<T>` do Spring): corpo tipado + status, headers e cookies.
- Respostas especiais: `FileResponse`, `StreamingResponse`, `RedirectResponse`.
- Erros: `raise HTTPError(status, detalhe)` → JSON `{"detail": ...}`.

### 5.5 Autenticação (v0.1)
- Estratégia configurada no `App(auth=...)`: `SessionAuth` (sessão em cookie assinado com HMAC) ou `JWTAuth` (HS256 via `hmac`; RS256/ES256 com o extra opcional `cryptography`).
- Cookies assinados também disponíveis fora da autenticação.
- `@Authenticated` e `@Roles(...)` na classe ou no método; no método, somam às restrições da classe. Sem autenticação → 401; sem o papel → 403.
- `Identity` (usuário autenticado) e `Session` (dict-like) injetáveis por tipo.
- OAuth (ex.: login com Google) fica para a v0.2+.

### 5.6 Middlewares
- Classes com `@Middleware(order=N)`, descobertas pelo mesmo `scan`; envolvem a requisição inteira via `call_next`.
- Menor `order` roda primeiro (camada mais externa). Instanciadas uma vez, sem argumentos.
- `App(middlewares=[...])` também é aceito, útil em testes.
- Prontos para uso: `CORS` e `GZip` (via `zlib` da stdlib).

```python
# myapp/middlewares/timing.py
import time

from featherweb import Middleware, Next, Request, Response


@Middleware(order=10)
class Timing:
    async def __call__(self, request: Request, call_next: Next) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["x-time"] = f"{time.perf_counter() - start:.4f}"
        return response
```

### 5.7 Tratamento de erros
- `raise HTTPError(status, detalhe)` ou subclasses com `status`/`detail` na própria classe.
- `@ExceptionHandler(Exc)` em métodos de um controller (local) ou de uma classe `@ControllerAdvice` (global, descoberta pelo scan).
- Resolução: handler local → global → padrão. Em cada nível vence a classe de exceção mais específica (MRO).
- O handler recebe a exceção (e pode declarar `Request`) e retorna um tipo anotado ou `Response[T]`, como qualquer método.
- Padrão: `HTTPError` → `{"detail": ...}` com o status; exceção não tratada → 500 com corpo genérico (traceback só no log, ou na resposta em modo debug).

```python
# myapp/errors.py
@ControllerAdvice
class GlobalErrors:
    @ExceptionHandler(PermissionError)
    async def forbidden(self, exc: PermissionError) -> Response[ErrorOut]:
        return Response(ErrorOut(detail=str(exc)), status=403)


# local: só vale dentro deste controller
@Route("/users")
class UserController:
    @ExceptionHandler(UserNotFound)
    async def not_found(self, exc: UserNotFound) -> Response[ErrorOut]:
        return Response(ErrorOut(detail="user not found"), status=404)
```

### 5.8 Execução
- CLI do framework (`argparse` da stdlib), instalada como script e também via `python -m featherweb`:
  `featherweb run myapp.app:app --host 0.0.0.0 --port 8000 --workers 4`
- `app.run(host=..., port=...)` para scripts; mesmo runner por baixo.
- Por ser ASGI, também roda em `uvicorn myapp.app:app`.

### 5.9 Em aberto
Configuração (arquivo e/ou variáveis de ambiente, como o `application.properties` do Spring) e ergonomia dos testes (`TestClient`).

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

**Scan de pacote**: importa módulos do usuário no startup; erro de import deve apontar o módulo; nunca roda por requisição.

**Autenticação**
- Comparações de assinatura com `hmac.compare_digest` (tempo constante).
- JWT: lista fixa de algoritmos aceitos (nunca `alg: none` nem o `alg` do token decidindo sozinho), validação de `exp`/`nbf`/`iat` com tolerância configurável, seguindo a RFC 8725.
- Cookies com `HttpOnly`, `Secure` e `SameSite=Lax` por padrão; suporte a rotação de chave (lista de segredos).

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
  5. Autenticação: vetores de teste das RFCs de JWT e casos de adulteração/expiração.
  6. (Opcional) Autobahn TestSuite para WebSocket.
- Benchmarks com `oha` (funciona no Windows): hello, JSON, path param, arquivo estático — comparando com Starlette/FastAPI sob uvicorn. Import time e memória também registrados.
- CI (GitHub Actions): Python 3.12/3.13/3.14 × Linux/Windows/macOS.

## 8. Fases

| Fase | Entrega | Pronto quando | Status |
|---|---|---|---|
| 0. Setup | `git init`, `uv init --lib`, ruff/pyright/pytest, CI | `uv run pytest` passa no CI | ✅ concluída localmente (CI aguarda o remoto) |
| 1. Servidor HTTP | `parser.py`, `protocol.py`, `runner.py` mínimos | App ASGI "cru" responde; keep-alive e pipelining testados; casos de smuggling rejeitados | ✅ concluída |
| 2. Núcleo do framework | `App`, `Router`, controllers (`@Route`, verbos, `scan`), `Request`, `Response`, `HTTPError`, `@ExceptionHandler`/`@ControllerAdvice`, `@Middleware`, lifespan, CLI, `TestClient` | Mesma suíte passa no servidor próprio e no uvicorn | ✅ concluída (entrada ainda limitada a path params e tipos do framework; a inferência completa da seção 5.3 é a Fase 3) |
| 3. Type hints | `params.py` (entrada) + `serialization.py` (retorno tipado, `Response[T]`), 422 | Regras das seções 5.3 e 5.4 cobertas por testes | ✅ concluída (`File()` e `FileResponse`/`StreamingResponse`, citados em 5.3 e 5.4, dependem do multipart e do streaming da Fase 4) |
| 4. Streaming e arquivos | `StreamingResponse`, `FileResponse`, `StaticFiles`, multipart | Upload de 100 MB sem estourar memória; 304/206 corretos | ✅ concluída (100 MB com pico de ~1,4 MB; 304/206/416 cobertos nos três stacks) |
| 5. WebSocket | `ws_protocol.py` + `websocket.py` + `@Ws` | Echo com cliente `websockets`; close/ping corretos | ✅ concluída (echo, ping/pong, close com código e razão e subprotocolo, verificados no servidor próprio e no uvicorn) |
| 6. Autenticação | cookies assinados, `SessionAuth`, `JWTAuth`, `@Authenticated`/`@Roles`, `Identity` | Login por sessão e por JWT; 401/403 corretos; tokens adulterados, expirados ou com `alg` inesperado rejeitados | ✅ concluída (inclui RS256/ES256 com o extra `crypto` e a recusa do ataque de confusão de algoritmo) |
| 7. Endurecimento e desempenho | timeouts, limites, graceful shutdown, workers, TLS, extras opcionais, profiling | Metas da seção 2 atingidas e registradas | ✅ concluída (medições na seção 2; import e linhas ficaram fora do alvo, com o porquê registrado) |
| 8. Release v0.1 | README, exemplos, docs da API, publicação no PyPI | `pip install featherweb` + exemplo do README funciona | ✅ publicada no PyPI como **1.0.0** em 2026-09-25, pelo workflow de release (ver [RELEASING.md](RELEASING.md)); o escopo é o do MVP v0.1, e a numeração 1.0 marca a API pública como estável |

## 9. Fora do escopo da v0.1 (candidatos a v0.2+)
HTTP/2 e HTTP/3 · OAuth (ex.: Google) · geração de OpenAPI (os type hints da Fase 3 já dão a base) · hot reload · `permessage-deflate` · parser C opcional (`httptools`) · rotas avulsas (`@app.get`).

Fora do escopo por decisão: container de injeção de dependências e integração com banco de dados.

## 10. Riscos
- **Parser próprio e segurança** → parsing estrito, limites conservadores, testes de smuggling, fuzz com hypothesis.
- **Autenticação própria** → seguir RFC 7519/8725, algoritmos fixos, comparação em tempo constante, testes com vetores conhecidos.
- **Parser em Python puro mais lento que C** → aceitar no MVP; medir; fast path opcional depois.
- **Diferenças Windows × Linux** (Proactor loop, sem uvloop, sem `SO_REUSEPORT`) → CI em matriz desde a Fase 0.
- **Overhead dos dicts ASGI** → medir na Fase 7; só otimizar se o benchmark justificar.
