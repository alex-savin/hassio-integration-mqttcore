# Notes for coding agents

Follow CONTRIBUTING.md. In short:

- Work on `develop` only. Never commit to `main`; GitHub refuses direct
  pushes to it, administrators included.
- Every change adds a line under `## Unreleased` in CHANGELOG.md.
- A release is a pull request from `develop` to `main`. Open it only when
  the owner says the release is ready: bump the manifest version, rename
  `## Unreleased` to `## <version> — <date>`, then open the pull request.
  Merging it publishes the GitHub release. Never tag or publish a release
  by hand.
- The repository has no license, by the owner's decision (2026-11-22). Do
  not add one. `validate.yml` skips the HACS license check until there is
  one.
- The repository is public. Keep site-specific names, addresses and tokens
  out of files and commit messages.
