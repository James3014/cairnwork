# Quickstart

Cairnwork is the governance layer around an AI (or any) code worker: it binds a task to one
owner, runs the candidate fix in an isolated clone, enforces the file scope, runs the tests, and
records a Core **VERIFIED** verdict bound to the exact commit. This page gets you from nothing to
a verified fix in a couple of minutes.

## 1. Install

The main path is one command — no `curl | sh`:

```sh
uv tool install --from git+https://github.com/James3014/cairnwork.git cairnwork
```

That puts `cairnwork`, `cairnwork-route`, `cairnwork-issues` and `cairnwork-verify` on your
`PATH`. ([uv](https://docs.astral.sh/uv/) is a single static binary; install it first if you do
not have it.) If you would rather not install uv by hand, a readable fallback script is published
alongside the releases (verify its `sha256` first):

```sh
curl -fsSLO https://cairnwork.github.io/install.sh   # read it, check the sha256, then:
sh install.sh
```

**Platform note.** Runtime is Python ≥ 3.11. Cairnwork depends on `cryptography`; if your
platform has no prebuilt wheel for the resolved version (notably older **Intel Macs**), pip/uv
builds it from source, which needs a Rust toolchain (`rustup`) first. Apple Silicon and
Linux x86-64/arm64 use prebuilt wheels.

## 2. See it work — the five-minute demo

```sh
cairnwork demo
```

This needs no GitHub, no AI worker and no API key (setting up the throwaway venv may fetch
pytest from the cache or index the first time). It builds a throwaway git project with a seeded
bug and a test that catches it, applies a ready-made one-line fix as the candidate, and asks
Cairnwork to verify it — the governed *verification* an attempt goes through. You should see the
Core verdict:

```
  verdict:      VERIFIED ✓
  core status:  VERIFIED
```

Pass `--keep` to keep the throwaway tree and inspect the isolated clone and logs.

The demo verifies a ready-made (deterministic) candidate so it is repeatable and needs no AI
worker. In the **real** Issue flow (below) the worker fixes the bug against **deferred tests it
never sees** — the governance Cairnwork adds — and only a Core VERIFIED verdict marks the task
done. The demo shows that verification machinery, not the test-hiding, which the real flow adds.

## 3. Point it at a real repository

Create a state directory (one per target repository) and run the Issue intake:

```sh
cairnwork init --dir ~/.cairnwork/<repo>            # writes config.json
cairnwork-issues --state ~/.cairnwork/<repo> --repo OWNER/NAME --clone <mirror> run-once
```

See [ISSUE_INTAKE.md](ISSUE_INTAKE.md) for how a task Issue is written (the `eia-task` block,
deferred `test_commit`, the `cairnwork:queue` label and the trusted-identity rules).

## Running untrusted third-party code

The `demo` runs verifiers under the account that launches Cairnwork (fine for this fixed,
self-contained fixture). `cairnwork init` **defaults to the dedicated account** (`verifier_account`
true); it falls back to the launching account only if you pass `--no-verifier-account`, which is
appropriate only for your own or semi-trusted code. To run genuinely adversarial code, first set
up the dedicated, unprivileged verifier account (macOS: `scripts/setup_verifier_user.sh`; Linux:
`scripts/setup_verifier_user_linux.sh`), then `cairnwork init` with the default account on.
See [SECURITY.md](SECURITY.md) for the sandbox backends and the isolation model.
