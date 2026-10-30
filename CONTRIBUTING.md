# Contributing

Bug reports, translations and pull requests are welcome in [HeeroML/ha-tado-x](https://github.com/HeeroML/ha-tado-x).

## Reporting a bug

1. Check the [troubleshooting table](README.md#troubleshooting) and the [open issues](https://github.com/HeeroML/ha-tado-x/issues).
2. Open an issue with the bug template. Attach the diagnostics of the entry (**Settings → Devices & services → Tado X → ⋮ → Download diagnostics**) and, if you can, a debug log. Both are free of tokens and account data; still read them before you post.
3. Never post tokens, sign-in codes, the sign-in link or the unredacted output of the live probe.

## Development setup

You need Python 3.14 and git. Docker is needed only for hassfest.

```bash
git clone https://github.com/HeeroML/ha-tado-x && cd ha-tado-x
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
```

`requirements_test.txt` pins `pytest-homeassistant-custom-component`, which pins the matching Home Assistant version, plus ruff and mypy.

## Checks

Run all of them before you open a pull request; CI runs the same.

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/python -m pytest --cov=custom_components.tado_x --cov-branch --cov-fail-under=95
.venv/bin/python scripts/gen_translations.py --check
docker run --rm -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest
```

`client/auth.py`, `client/ratelimit.py`, `client/http.py`, `budget.py`, `termination.py`, `migration.py` and `config_flow.py` must stay at 100 % line coverage (see `.github/workflows/tests.yml`).

## Code rules

- Write code the way Home Assistant core does: typed (mypy strict), small functions, `EntityDescription` with value functions, docstrings on public symbols, comments only for a non-obvious reason.
- No `from __future__ import annotations`. Use `probatio` for schemas, not `voluptuous`.
- `custom_components/tado_x/client/` must not import Home Assistant or its parent package; `budget.py` and `termination.py` must not import Home Assistant. `tests/test_architecture.py` checks this.
- Never log tokens, codes, the sign-in link, the `Authorization` header or account data. `tests/test_logging.py` checks the main paths.
- Every API call must fit the budget model: a new polled endpoint needs an entry in `budget.py`, an option if it is optional, and its calls per day in the option description.
- Keep entity unique ids stable. Changing one loses the user's history.

## Tests

- Tests go through HTTP: use `TadoCloudMock` from `tests/tado_mock.py` (or `aioclient_mock`) and move time with `freezer`. Do not patch integration code.
- Fake tokens only. Fixtures in `tests/fixtures/` are synthesized; ids, names and serial numbers are invented.
- Platform tests use snapshots (`tests/snapshots/`). Update them with `--snapshot-update` and review the diff.

## Translations

- `custom_components/tado_x/strings.json` is the English source. Do not edit `translations/en.json`; regenerate it with `.venv/bin/python scripts/gen_translations.py`.
- `translations/de.json` must stay complete; the tests fail on a missing key. It uses the informal "du", like Home Assistant.
- `fr.json` and `it.json` may be partial; missing keys fall back to English. `scripts/gen_translations.py --report` lists the gaps.
- Placeholders such as `{home}` must match the English string. State and option keys are lowercase.

## Commits and pull requests

- One logical change per commit, with a message that says what changed and why.
- Describe user-visible changes for the [changelog](CHANGELOG.md), and call out breaking changes.
- Keep the README in sync when you add or change an option, entity or action.

## Live API probe

`scripts/live_probe.py` reads the live API of your own account (GET only) and writes a pseudonymized report; see the README. If you share a report, read it first and never share the `--keep-raw` file.

## Releasing

1. Set `version` in `custom_components/tado_x/manifest.json` and add a `## <version>` section to the changelog. `python3 scripts/release_check.py v<version>` checks both.
2. Push the tag `v<version>`. Do not create the release on GitHub by hand.

The release workflow checks the tag against the manifest, runs lint and tests, and only then creates the release with `tado_x.zip` and the changelog section as its notes. A manual run of the workflow with a tag is a dry run that creates nothing.

## License

By contributing you agree that your contribution is licensed under the MIT License of this repository.
