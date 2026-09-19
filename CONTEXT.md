# Development Context

Repo Observatory is an independent extraction of Cache Observatory. The SGLang cache profile retains the original UI and CSS. Keep visual changes scoped to explicitly requested controls.

- Runtime: Python standard library HTTP server, static HTML/JS/CSS, JSON snapshots, SQLite invited access.
- Workspace: one configurable repository per instance; use separate processes and data directories for additional repositories. A combined multi-repository UI is out of scope for this version.
- Language: Chinese and English, selectable on dashboard and account pages. Do not translate GitHub source or editorial text. Preserve activation tokens when switching languages before account activation.
- Analysis: trusted local provider interface, disabled by default. Results are separate from source facts and are not shown in the UI. No model credentials or provider are bundled.
- Data: configuration examples belong in Git; accounts, invitations, keys, tokens, snapshots, private notes, and machine-specific deployment configuration do not.
- Verification: Python unit/HTTP tests and Playwright with temporary real servers and recorded public fixtures. Screenshots and runtime output are ignored.
- Repository presentation: README uses reviewed, unmodified public-fixture screenshots in `docs/images/`. Generated captures remain under ignored `artifacts/`; never publish account or invitation screens. Updating the README does not authorize changing the application UI.

The original deployed instance is not migrated by this extraction. Deployment requires a separate operational change preserving persistent accounts and data. Do not assume any external service state from this repository.
