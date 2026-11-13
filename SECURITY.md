# Security policy

This project trades on **paper and demo accounts only**. Live trading is blocked in code
([`src/trader/runtime/live_guard.py`](src/trader/runtime/live_guard.py)), and the console is a
read-only page bound to `127.0.0.1`. Security reports are still welcome, because a mistake in a
trading system is expensive wherever it runs.

## Reporting a vulnerability

Report it privately through GitHub: open the **Security** tab of this repository and choose
**Report a vulnerability**. Please do not open a public issue for a vulnerability or a leaked key.

Useful to include: what you found, the file and line if you have them, how to reproduce it, and
what an attacker could do with it.

You can expect an acknowledgement within a week. This is a one-person project, so fixes are
made on a best-effort basis, on the `main` branch only.

## What counts

- Any path that could send an order to a live account, or load a live key.
- A secret (API key, password, database URL) that reaches a log, a manifest, a report, the
  research registry or the console.
- A way for a model, a feed or the AI assistant's tools to reach order authority. The
  boundaries are tested in [`tests/test_boundaries.py`](tests/test_boundaries.py).
- A way to make the console or the Desk answer on a non-loopback address, or to write through
  the read-only database role.
- A way to alter the research registry or a practice journal without breaking its hash chain.

## If you find a secret in this repository

Report it privately as above. Removing a key from git history is not enough; the key has to be
revoked at the provider.

## Using this project safely yourself

- Use your **own paper or demo keys**. Never put a live key into this project.
- Keys live in the operating system's keyring or in your local `.env`, which is gitignored.
  See [CONTRIBUTING.md](CONTRIBUTING.md) section 2.
- If you paste a key anywhere public, revoke it at the provider straight away.
