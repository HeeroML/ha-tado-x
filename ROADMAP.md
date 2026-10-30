# Roadmap

Plans for the HeeroML fork after 1.9.0. Items are ordered by priority. Nothing here has a date.

## Before or right after the 1.9.0 release

- **Live verification.** Run `scripts/live_probe.py` against a real tado X home and settle the open points in [docs/API.md](docs/API.md#open-questions-for-a-live-check). Replace the synthesized test fixtures with the probe's `--emit-fixtures` output.
- **Manual write test** on a real home: the room termination PATCH, manual control with each termination, presence lock removal, and open window mode without a detection.

## Next

- **Shared room and device entity bases** in `entity.py`, to remove repetition across the platforms and bring `climate.py` and `button.py` back within their size budgets.
- **French and Italian translations** for the strings added in 1.9 (missing strings fall back to English). Contributions from native speakers are welcome.
- **Spec follow-up:** document `TadoXHome.async_read_holiday_mode()` (a command-priority read used by the holiday mode actions) in the architecture notes.

## Later, if there is demand

- **Domestic hot water** (upstream PRs #53 and #74). Needs someone with a tado X hot water setup to test. The endpoints are in [docs/API.md](docs/API.md#domestic-hot-water-not-implemented).
- **Away temperature per room** (`settings/away/rooms/{room}`), single-source so far.
- **Publishing the client** (`custom_components/tado_x/client`) as a separate package; it already has no Home Assistant imports.

## Not planned

- **Local control over Matter or Thread.** tado X devices can join Home Assistant's own Matter integration. This integration stays a cloud integration and does not duplicate that.
- **An update interval option.** Polling follows the daily call budget; see the README.
- **Support for V3+ homes.** They belong to the core tado integration.
