# Releasing

Everything up to the upload can be done and checked locally. The upload itself is not
scripted here on purpose: publishing to PyPI is irreversible — a version number can be
yanked but never reused — and it goes out under your account, so it is a command you run
yourself, having looked at what you are about to send.

## Before tagging

```sh
uv run ruff check && uv run ruff format --check
uv run pyright
uv run pytest
python benchmarks/measure.py            # the targets in PLAN.md section 2
```

Then check by hand:

- [ ] `src/featherweb/__init__.py` has the version you mean to release.
- [ ] `PLAN.md` section 8 reflects what is actually done.
- [ ] The README's opening example still runs (see below — it is not in the test suite).
- [ ] The name is still free, or still yours: `curl -s -o /dev/null -w '%{http_code}' https://pypi.org/pypi/featherweb/json` (404 means free).

## Build and verify

```sh
rm -rf dist
uv build                                # wheel + sdist into dist/
```

Install what you built into an empty environment and run the README example against it —
this is the release gate from the plan, and it catches things the test suite cannot, such
as a module missing from the wheel:

```sh
uv venv /tmp/fresh --python 3.12
uv pip install --python /tmp/fresh/bin/python dist/featherweb-*.whl
/tmp/fresh/bin/python - <<'PY'
import featherweb
print(featherweb.__version__, len(featherweb.__all__), "public names")
PY
```

Worth a look inside the artifacts:

```sh
python -c "import zipfile; print(*zipfile.ZipFile('dist/featherweb-1.0.0-py3-none-any.whl').namelist(), sep='\n')"
```

- [ ] `featherweb/py.typed` is in the wheel, or type checkers ignore the annotations.
- [ ] `LICENSE` is in the wheel.
- [ ] `Requires-Dist` lists nothing outside the `crypto` extra.

## Publish

Publishing is done by `.github/workflows/release.yml` when a `v*` tag is pushed. It runs
the whole CI first, builds, refuses to go on if the tag does not match `__version__`, and
uploads with [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — there is no
token to leak, because PyPI trusts this repository's workflow directly.

One-time setup:

1. On PyPI, *Your account → Publishing → Add a new pending publisher*: project
   `featherweb`, owner `lucas-lmoura`, repository `featherweb`, workflow `release.yml`,
   environment `pypi`. After the first upload it becomes the project's publisher.
2. On GitHub, *Settings → Environments → New environment* named `pypi`. Adding yourself as
   a required reviewer there makes every release wait for a click before it uploads.

Then, for each release:

```sh
git tag -a v1.0.0 -m "featherweb 1.0.0"
git push origin main --tags
```

A failed run uploads nothing, so a wrong tag is fixed by deleting it
(`git push origin :refs/tags/v1.0.0`) and tagging again. A successful one cannot be
undone: a version number can be yanked but never reused.

### By hand, if the workflow is unavailable

Test PyPI first, which is free to get wrong:

```sh
uv publish --publish-url https://test.pypi.org/legacy/ dist/*
uv pip install --index-url https://test.pypi.org/simple/ featherweb
```

Then the real one, with an API token scoped to this project (`__token__` as the username):

```sh
uv publish dist/*
```

## After

Bump `__version__` to the next development version, so the released number never points
at a working tree that has moved on.
