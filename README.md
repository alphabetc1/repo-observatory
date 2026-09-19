# Repo Observatory

A self-hosted GitHub issue and pull request workspace, extracted from Cache Observatory. It tracks open issues, PRs, RFCs, ownership, reviews, merge conflicts, and current-commit CI. The original dashboard layout is retained.

Python 3.10+ and the standard library are sufficient at runtime. Node is only needed for verification. No LLM or external translation service is used by default.

## Run locally

```sh
export OBSERVATORY_CONFIG="$PWD/configs/sglang.json"
python3 sync.py --data-dir data/sglang --use-gh
python3 pr_status.py --data-dir data/sglang --use-gh
python3 server.py --data-dir data/sglang --port 8787
```

Open http://127.0.0.1:8787. `--use-gh` uses an existing GitHub CLI login locally. Alternatively set `GITHUB_TOKEN` in the process environment. Never commit credentials. The initial sync can require many GitHub requests; later syncs are incremental. Schedule entry sync every three hours and workflow collection every 30 minutes using your scheduler; the application does not install timers.

## Configuration

`OBSERVATORY_CONFIG` selects a JSON configuration. The SGLang profile preserves the original cache modules. Copy `configs/example.json` to a gitignored `*.local.json` to watch another repository and configure module regexes, a CI workflow, event, and default language.

Each running workspace watches **one repository**. Run separate processes, ports, and data directories for multiple repositories. A combined cross-repository dashboard is not implemented. Entries expose a repository-qualified `id`; state files reject a different repository to prevent accidental reuse. Do not change a workspace repository while it is running.

Manual notes live in `<data-dir>/editorial.json`, keyed by issue number within that workspace. Runtime snapshots and account databases are never source files. A failed collection retains the previous complete snapshot.

## Languages

Choose **中文 / English** in the dashboard sidebar or account pages. The preference is stored in a same-origin cookie and survives reloads; `default_language` controls the initial setting. Application labels, errors, methodology, and rule explanations are localized. GitHub titles, bodies, author text, and manual notes retain their original language.

`locales/en.json` contains application-copy translations. The server renders the existing HTML/JS assets in the selected language without duplicating the UI. JSON localization only touches application-owned fields. Stylesheets and the dashboard structure are preserved; only the language control is added.

## Optional analysis interface

`analysis.py` defines `Analyzer.analyze(AnalysisRequest) -> AnalysisResult`. Set `analyzer` to a **trusted local** `module:factory` to opt in. The factory returns an analyzer. No vendor SDK, model, network endpoint, API key, or paid call is configured.

Results include summary, rationale, suggested priority, provider, model, and version, with source timestamp and language attached by the pipeline. They are stored in `entry.analysis`; GitHub facts, rule priorities, and editorial notes remain authoritative and unchanged. The current UI does not display LLM results. Provider failure records `status: unavailable` and preserves rule-based results without publishing provider exception text.

Before implementing a network provider, add a bounded timeout, source/version-based caching, a cost limit, and provider-specific credential handling outside the repository. Treat issue text as untrusted input. Only configure a provider when sending the watched content to it is authorized. The interface currently runs synchronously during collection.

## Invited access

The server binds to loopback. Local development without authentication must not be exposed through a public proxy. For invited access, set `CACHEBOARD_PUBLIC_ORIGIN` to your HTTPS origin before starting the server, then initialize an administrator:

```sh
python3 access.py --data-dir data/sglang --origin https://observatory.example --bootstrap owner --output data/sglang/owner-setup.json
```

Replace the example origin with your own origin, matching `CACHEBOARD_PUBLIC_ORIGIN`. Store the generated invitation privately; do not include it in logs, commits, or screenshots. Deploy behind your own HTTPS reverse proxy and preserve the SQLite account database and runtime data on updates. This repository contains no host-specific deployment configuration, keys, certificate material, or personal operational notes.

## Verification

```sh
python3 -m unittest discover -s tests -v
npm ci
npx playwright install chromium
npm run test:browser
```

The browser test starts temporary local servers and uses sanitized, recorded public GitHub fixture data. It exercises both languages, details, filters, responsive layouts, and the real invitation/login boundary without contacting a deployed instance. Screenshots are written to gitignored `artifacts/`.
