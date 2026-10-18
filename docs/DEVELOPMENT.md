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

Linux or WSL:

```bash
python3 -m venv .venv-wsl
.venv-wsl/bin/python -m pip install -e '.[dev]'
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

Use `.venv-wsl/bin/python` on Linux or WSL. Without this extra, tests that require aiohttp are skipped at
collection. Official-template corpus and external integration campaigns also require their
separately acquired inputs; reference repositories and maintainer evidence are not distributed.

## Run focused suites

Windows PowerShell:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests
pnpm.cmd --dir frontend run test
pnpm.cmd --dir frontend run test:e2e:ci
```

Use `.venv-wsl/bin/python` and `pnpm` on Linux or WSL. Add a test path or selector to run a focused subset.
Frontend unit tests use Vitest; the ordinary browser suite uses Playwright and local host doubles.
Test collection alone does not establish a passing execution result.

## Run the standard repository checks

The Windows runner checks artifact integrity, pre-commit hooks including secret scanning, frontend
formatting and types, frontend units, backend product regressions, security and a bounded browser
smoke selection:

```powershell
powershell -File scripts/run_full_tests_windows.ps1
```

On Linux or WSL:

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

## After changing contracts, nodes or generators

Some tracked files are generated from others: contract records, inventories, fingerprints, build
provenance and the supply-chain manifest. The standard checks compare them with a fresh
computation, so they fail until the generated files are brought up to date. Regenerate after
changing:

- a node's sockets or the node contract registry;
- a contract JSON or JSON Schema;
- frontend source, which build provenance hashes;
- a script that writes a generated file (one with a `--write` option);
- the set of tracked files a generator scans, for example by adding a module or a test.

`scripts/derived_artifacts.py` declares every generator and the order in which they run. First
stage new files with `git add`, because some generators ask Git which files exist. Then preview the
regeneration and apply it:

```powershell
& .\.venv\Scripts\python.exe scripts/derived_artifacts.py dry-run
& .\.venv\Scripts\python.exe scripts/derived_artifacts.py apply
```

Use `.venv-wsl/bin/python` on Linux or WSL. The commands behave as follows:

- `dry-run` computes every output in a scratch copy under `.tmp/`, writes no final file, and
  reports what would change and why.
- `apply` validates the whole set before it replaces anything.
- `plan` prints the run order.
- `describe` prints the full declaration.

If the output reports `bundle rebuild implied`, rebuild the frontend with
`pnpm --dir frontend run build`. Apply derived artifacts before the build, stage the new bundle,
then apply derived artifacts again so build provenance binds the final bundle bytes.

A rebuilt bundle also moves frontend expectation pins that no generator updates. Update affected
expectations and include them in the same commit as the bundle.

Do not edit a generated file by hand. When a generator's `--check`, or a test that compares a
generated file with a fresh scan, fails, regenerate the file instead of changing the expected
value.

Contract files live in two directories:

- `comfyui_h3_context/contracts/` is part of the installed package. Put a file there only when the
  product reads it at run time or compiles it into the frontend bundle.
- `governance/contracts/` holds developer records, inventories and schemas. It is not installed
  with the package. The public repository and source distributions carry it for the tests and
  tooling that read it.

`governance/contracts/packaged_artifact_readership_v1.json` records the reader of each packaged
file. Consult it before you move or delete a packaged file: a file's name or format does not show
who reads it. `scripts/derived_artifacts.py bundle-inputs` lists the files that the frontend bundle
compiles in.

After regeneration, stage the outputs and run `dry-run` again. A stable result reports no pending
changes. Run the affected suites before the standard repository checks. Browser tests use the
hermetic Playwright commands described above; live-host qualification has separate prerequisites.

## Optional integrations

Supported-host, rendering and provider qualification tests require explicitly configured external
prerequisites. Missing prerequisites can produce skipped or not-run rows. Those results do not
prove the integrations work. Ordinary hermetic tests do not authorize using provider credentials,
installing models, changing a running ComfyUI host or queueing generation.

## Verify distribution completeness

`scripts/product_completeness.py` checks source-declared runtime and developer inputs and performs
an isolated product import/resource smoke. `scripts/registry_payload.py` audits the actual packed
archive against the tracked source. `scripts/registry_policy_check.py <archive.zip>` is an offline
check that packaged runtime sources do not download, extract or install executables; it reads
archive members in memory and runs none of them. Missing tests, fixtures, configurations, tooling or runtime
resources fail these checks; private records and generated dependency/cache material stay excluded.

Build a Python source distribution from a Git checkout whose reviewed public inputs are tracked:

```powershell
& .\.venv\Scripts\python.exe -m build --sdist
& .\.venv\Scripts\python.exe scripts/sdist_payload.py --archive dist/minimax_h3_studio-1.3.2.tar.gz
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

## Release validation

CI runs for `dev` and `release-candidate/**` pushes, pull requests and explicitly requested
manual dispatches. A release candidate uses its first workflow run, attempt 1, with seven
required jobs successful. The Windows browser job must be present and completed on that same
run and commit; its conclusion is advisory and reported for release review. All eight jobs
remain required in the matrix. A retry or a second run of the same commit does not provide release
approval. Correct a failed candidate and validate the resulting meaningful new commit once.

Use the release push guard to validate the exact public projection, live public parent,
version authorities, outgoing references and original CI proof before pushing. The preparation
controller remains token-free and does not push unless a guarded push operation is invoked.
After approval, promote the qualified candidate commit to `main` together with its new annotated
version tag atomically. That promotion does not start another CI run; Registry publication keeps
its separate version-change workflow. Maintenance follows the same candidate-first validation.
