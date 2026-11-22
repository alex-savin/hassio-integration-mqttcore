# Working on the MQTT Core integration

## Branches

- **`develop`** holds all work. Commit and push there. The Tests and
  Validate workflows run on every push.
- **`main`** holds released code only. It changes only through a pull
  request from `develop`, opened when a release is ready. GitHub enforces
  this for everyone, administrators included: `main` takes no direct push,
  and a pull request into it merges only when its checks pass. Pull
  requests merge with a merge commit (squash and rebase are off), so
  `develop` and `main` keep one history.

## Every change

1. Commit it on `develop`.
2. Add a line under `## Unreleased` in `CHANGELOG.md`. Write it for someone
   deciding whether to update: what changes for them, not how the code
   changed.
3. Push.

## A release

1. On `develop`, set the new version in
   `custom_components/mqttcore/manifest.json`
   ([semantic versioning](https://semver.org): a fix raises the last
   number, a new feature the middle one).
2. In `CHANGELOG.md`, rename `## Unreleased` to `## <version> — <date>`,
   with the date as `YYYY-MM-DD`.
3. Commit as `<version>: <what it brings>` and push.
4. Open the pull request:
   `gh pr create --base main --head develop --title "<version>"`, with the
   changelog section as its description.
5. When its checks pass, merge it.

The Release workflow then tags `v<version>` on `main` and publishes the
GitHub release with the changelog section as its notes. The Marketplace
offers the release to every installation. The workflow also moves
`develop` up to `main`.

The pull request's **Release check** refuses it when:

- the manifest's version has no changelog section, or the section is empty;
- that version was released before (`v<version>` exists);
- `## Unreleased` still has entries.

## Checks before pushing

```sh
ruff format --check . && ruff check . && mypy && pytest
```

This is a public repository. Keep site-specific names, addresses and tokens
out of the code, the test fixtures and the commit messages: use
`example.com` names and documentation addresses.
