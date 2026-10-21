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

**Platform note.** Runtime is Python ≥ 3.11. `cryptography` 50 ships wheels for Apple Silicon and
for Linux x86-64/arm64; on an **Intel Mac** there is no wheel, so it builds from source and you
need a Rust toolchain (`rustup`) first.

## 2. See it work — the five-minute demo

```sh
cairnwork demo
```

This needs no GitHub, no network and no AI worker. It builds a throwaway git project with a
seeded bug and a test that catches it, applies a ready-made one-line fix as the candidate, and
asks Cairnwork to verify it. You should see the Core verdict:

```
  verdict:      VERIFIED ✓
  core status:  VERIFIED
```

Pass `--keep` to keep the throwaway tree and inspect the isolated clone and logs.

## 3. Point it at a real repository

Create a state directory (one per target repository) and run the Issue intake:

```sh
cairnwork init --dir ~/.cairnwork/<repo>            # writes config.json
cairnwork-issues --state ~/.cairnwork/<repo> --repo OWNER/NAME --clone <mirror> run-once
```

See [ISSUE_INTAKE.md](ISSUE_INTAKE.md) for how a task Issue is written (the `eia-task` block,
deferred `test_commit`, the `cairnwork:queue` label and the trusted-identity rules).

## Running untrusted third-party code

The demo and `init` default to running verifiers under the account that launches Cairnwork,
which is fine for your own or semi-trusted code. To run genuinely adversarial code, first set up
the dedicated, unprivileged verifier account (macOS: `scripts/setup_verifier_user.sh`; Linux:
`scripts/setup_verifier_user_linux.sh`), then `cairnwork init` without `--no-verifier-account`.
See [SECURITY.md](SECURITY.md) for the sandbox backends and the isolation model.
