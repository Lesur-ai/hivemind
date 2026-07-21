# Contributing to Hivemind

Thank you for helping improve Hivemind.

The public `main` branch is a released source snapshot. Day-to-day development
of upcoming changes happens in a separate maintainer-controlled engineering
environment. Public issues and pull requests are therefore the community
intake for a future release, not a live view of unreleased development.

## Before you contribute

- Read the [support guide](SUPPORT.md) for questions and troubleshooting.
- Read the [security policy](SECURITY.md) before reporting a suspected
  vulnerability. Do not disclose security details in a public issue or pull
  request.
- Search existing issues and pull requests before opening a new one.
- Keep proposed changes focused and explain the user-visible outcome.

## Local setup

Install Python 3.11 or newer and
[`uv`](https://docs.astral.sh/uv/getting-started/installation/). Docker is
optional unless you are changing an image or deployment behavior. Then create
an isolated development environment and run the public checks:

```bash
git clone https://github.com/Lesur-ai/hivemind.git
cd hivemind
uv sync --locked --dev
uv run pytest tests/ -q
uv run python scripts/check_doc_links.py
git diff --check
```

Changes that affect a container build should also prove each relevant image
locally. These commands do not publish an image:

```bash
docker build -t hivemind:contributor .
docker build -t hivemind-waf:contributor waf
docker build -t hivemind-graph-memory:contributor services/graph-memory
```

Run the default public test suite without production credentials. Individual
integration or operator proofs may require Docker, an LLM provider, or external
datastores; their documentation identifies those prerequisites. Never use
production secrets in a test or pull request.

## Issues

Use the issue forms for reproducible bug reports, feature proposals, and
support questions. Include the Hivemind version or commit, deployment shape,
commands or steps that reproduce the behavior, expected and observed results,
and the smallest useful logs or test case. Remove credentials, tokens, and
private data before posting.

An accepted issue may be investigated and scheduled for a later release. An
issue is not a promise that a change will be included in the next snapshot.

## Pull requests

Pull requests should include a concise summary, reproducible evidence, the
commands you ran, tests for behavior changes, and documentation updates when
relevant. A submitted patch is not imported or merged automatically:
maintainers verify it, review it, and test it before deciding whether it belongs
in a future release.

When a contribution is accepted, maintainers preserve the original commit
author and the public pull request or issue credit in the release notes. The
change appears in a subsequent sanitized source snapshot rather than being
silently treated as a development branch.

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you
agree that maintainers may ask for a narrower reproducer or a revised patch
when that is needed to verify the change safely.
