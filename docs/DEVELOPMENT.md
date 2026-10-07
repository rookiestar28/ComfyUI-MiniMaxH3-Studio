# Development and testing

The public Git repository and Comfy Registry archive include backend and frontend tests, synthetic
fixtures, test configurations and the tooling they use. Private planning, reference checkouts,
maintainer records, dependency installations and generated test results are excluded.

## Prepare an environment

Run commands from the package root. Use Python 3.10 or newer, a project-local virtual environment,
Node.js 24 and pnpm 11.3.0, as declared in `frontend/package.json`.

Windows PowerShell:

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e '.[dev]'
pnpm.cmd --dir frontend install --frozen-lockfile
pnpm.cmd --dir frontend exec playwright install chromium
```

Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend exec playwright install chromium
```

These development dependencies are separate from installation of the node pack. They do not
install model weights. Downloaded source archives need a local Git checkout for tools that inspect
tracked files: clone the repository, or initialize and commit the extracted public source locally.

For hermetic host-route tests, install the optional `host-tests` extra as well:

```powershell
& .\.venv\Scripts\python.exe -m pip install -e '.[dev,host-tests]'
```

Use `.venv/bin/python` on Linux. Without this extra, tests that require aiohttp are skipped at
collection. Official-template corpus and external integration campaigns also require their
separately acquired inputs; reference repositories and maintainer evidence are not distributed.

## Run focused suites

Windows PowerShell:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests
pnpm.cmd --dir frontend run test
pnpm.cmd --dir frontend run test:e2e:ci
```

Use `.venv/bin/python` and `pnpm` on Linux. Add a test path or selector to run a focused subset.
Frontend unit tests use Vitest; the ordinary browser suite uses Playwright and local host doubles.
Test collection alone does not establish a passing execution result.

## Run the standard repository checks

The Windows runner checks artifact integrity, pre-commit hooks including secret scanning, frontend
formatting and types, frontend units, backend product regressions, security and a bounded browser
smoke selection:

```powershell
powershell -File scripts/run_full_tests_windows.ps1
```

On Linux:

```bash
bash scripts/run_full_tests_linux.sh
```

The standard gate runs a browser smoke selection, not every browser or live-host campaign. List the
selection without launching a browser:

```powershell
& .\.venv\Scripts\python.exe scripts/gate_stages.py browser-smoke --list
```

The secret scanner uses `requirements/secret-scan-baseline.json`, which contains reviewed finding
hashes and relative filenames, not credential values. A baseline is not permission to add secrets.

## Optional integrations

Supported-host, rendering and provider qualification tests require explicitly configured external
prerequisites. Missing prerequisites can produce skipped or not-run rows. Those results do not
prove the integrations work. Ordinary hermetic tests do not authorize using provider credentials,
installing models, changing a running ComfyUI host or queueing generation.

## Verify distribution completeness

`scripts/product_completeness.py` checks source-declared runtime and developer inputs and performs
an isolated product import/resource smoke. `scripts/registry_payload.py` audits the actual packed
archive against the tracked source. Missing tests, fixtures, configurations, tooling or runtime
resources fail these checks; private records and generated dependency/cache material stay excluded.

Build a Python source distribution from a Git checkout whose reviewed public inputs are tracked:

```powershell
& .\.venv\Scripts\python.exe -m build --sdist
& .\.venv\Scripts\python.exe scripts/sdist_payload.py --archive dist/minimax_h3_studio-1.1.0.tar.gz
```

The standard build backend copies public tracked files into a fresh temporary tree and respects
the candidate's Git ignore rules. Untracked fixtures must be reviewed and added to Git before
building. Suffixes do not determine inclusion: test helpers, images and compressed fixtures are
included along with their consumers. Local records, dependency installations and stale build
metadata cannot supply additional files.

An issued sdist includes `public-source-inventory.json`, a list of public paths and SHA-256 hashes.
It can be rebuilt without a Git checkout; extra files placed beside the extracted source are
excluded, and changes to listed bytes fail verification. For development edits, initialize a Git
checkout and track the reviewed inputs. The hashes detect changed inputs; they do not authenticate
an archive from an untrusted source. Editable installation continues to use the original checkout.
