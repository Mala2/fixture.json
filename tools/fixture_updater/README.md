# Soccer fixture updater

This updater makes one scheduled API-Football request, converts the provider
response into the existing ESP32 schema, validates it, and atomically replaces
the repository-root `fixture.json` only when the meaningful fixture data
changes.

```text
API-Football
  -> GitHub Actions
  -> fixture.json
  -> raw.githubusercontent.com
  -> LilyGO T-RGB
```

The API key exists only as a GitHub Actions secret or local environment
variable. It is never placed in firmware, JSON, command-line arguments,
provider samples, logs, or Git history. This is a six-hour next-fixture
prototype, not a live-score updater.

## Provider configuration

Create an API-Football account through the
[API-Football dashboard](https://dashboard.api-football.com/register). Use the
dashboard's team search or Live Tester to find Al Hilal and record its numeric
team ID. Do not repeatedly perform team or league discovery in the scheduled
workflow.

Required environment variables:

- `API_FOOTBALL_KEY`: secret API-Sports key.
- `API_FOOTBALL_TEAM_ID`: positive numeric team ID.

Optional variables and defaults:

- `FIXTURE_OUTPUT_PATH=fixture.json`
- `FIXTURE_REFRESH_AFTER_SECONDS=21600`
- `TARGET_TEAM_SLUG=al-hilal`
- `TARGET_TEAM_SHORT_NAME=HIL`

`API_FOOTBALL_TEAM_ID` deliberately has no default. Verify it in the provider
dashboard before enabling the workflow. The updater requests:

```text
GET /fixtures?team=<numeric-team-id>&next=1
```

over `https://v3.football.api-sports.io` with the `x-apisports-key` header,
finite timeouts, a one-megabyte response limit, and at most one retry for
temporary network or HTTP 5xx failures. Validation errors and HTTP 4xx
responses are not retried.

## Local setup

Python 3.12 and its standard library are the only dependencies:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --requirement tools/fixture_updater/requirements.txt
python -m unittest discover -s tools/fixture_updater/tests -p "test_*.py" -v
```

Set `API_FOOTBALL_TEAM_ID` and securely export `API_FOOTBALL_KEY` in the local
shell. Do not save the key in a checked-in `.env` file or shell script.

Network dry run:

```sh
python tools/fixture_updater/update_fixture.py --dry-run
```

Saved-response run without a network call or API key:

```sh
API_FOOTBALL_TEAM_ID=2939 \
python tools/fixture_updater/update_fixture.py \
  --provider-sample tools/fixture_updater/tests/fixtures/api_football_next_fixture.json \
  --output /tmp/fixture.json
```

Validate any generated file independently:

```sh
python tools/fixture_updater/validate_output.py /tmp/fixture.json
```

The sample's `2939` ID is test data derived from the earlier prototype payload;
verify the production ID in API-Football before configuring Actions.

## Output contract

The output contains only:

- schema version `1`;
- updater UTC generation time;
- refresh interval;
- selected team;
- provider-backed fixture ID;
- competition, UTC kickoff, and venue;
- home/away designation and teams; and
- one firmware-supported normalized status.

Provider IDs, logos, scores, league metadata, and raw provider responses are
not copied into the device document. Team IDs are stable lowercase slugs. A
fixture ID uses `api-football-<provider-id>`.

Timestamps must be timezone-aware provider values. They are converted to
`YYYY-MM-DDTHH:MM:SSZ`; local display conversion remains exclusively on the
ESP32.

Before replacement, the updater checks exact keys, JSON types, firmware string
limits, timestamps, enum values, selected-team consistency, UTF-8
serialization, the 4,096-byte device limit, and a serialize/parse/validate
round trip. It writes a same-directory temporary file, flushes it, and uses
atomic replacement.

Changing `generated_at` alone is ignored. The existing file is left untouched
and no commit is created unless fixture content changes.

## Status mapping

| API-Football codes | Device status |
| --- | --- |
| `TBD`, `NS` | `scheduled` |
| `1H`, `HT`, `2H`, `ET`, `BT`, `P`, `SUSP`, `INT`, `LIVE` | `live` |
| `FT`, `AET`, `PEN` | `finished` |
| `PST` | `postponed` |
| `CANC`, `ABD`, `AWD`, `WO` | `cancelled` |

An unknown code is a validation failure and preserves the existing JSON.

## Team abbreviations

`team_aliases.json` is checked first. The configured target-team abbreviation
is second. Otherwise, a deterministic two-to-four-character uppercase ASCII
fallback uses multiword initials or the first three meaningful characters.
Add exact provider team names to the alias file when a preferred abbreviation
is known.

## Failure behavior

- No upcoming fixture exits with code `3` and preserves the previous file.
- Provider/HTTP/envelope failure exits with code `4`.
- Normalization/output validation failure exits with code `5`.
- Configuration failure exits with code `2`.
- Raw provider responses are never written by production code.
- A failed run cannot partially replace `fixture.json`.

A separate no-fixture device schema can be designed later. This updater never
generates fake or empty fixture content.

## GitHub Actions setup

In `Mala2/fixture.json`, add the API key under **Settings → Secrets and
variables → Actions → Secrets → New repository secret**:

```text
API_FOOTBALL_KEY
```

Add this required repository variable under the adjacent **Variables** tab:

```text
API_FOOTBALL_TEAM_ID=<verified numeric team ID>
```

Optional repository variables are
`FIXTURE_REFRESH_AFTER_SECONDS`, `TARGET_TEAM_SLUG`, and
`TARGET_TEAM_SHORT_NAME`.

With GitHub CLI, the equivalent setup is:

```sh
gh secret set API_FOOTBALL_KEY --repo Mala2/fixture.json
gh variable set API_FOOTBALL_TEAM_ID --repo Mala2/fixture.json --body "<verified-numeric-id>"
```

`gh secret set` prompts for the secret without putting it in the command.

The workflow runs at minute 17 every six hours and supports manual execution
from **Actions → Update next fixture → Run workflow**. It grants only
`contents: write`, cancels an older overlapping run, runs all offline tests,
updates and validates `fixture.json`, and commits only a meaningful change.

Inspect logs for the sanitized request path, fixture summary, validation
result, and optional numeric quota remaining. Logs must never contain the
`x-apisports-key` value or provider headers.

After a successful changed run, verify:

```sh
curl --fail --silent --show-error \
  https://raw.githubusercontent.com/Mala2/fixture.json/refs/heads/main/fixture.json |
python -m json.tool
```

Then confirm the fixture ID and kickoff in the raw document. GitHub's raw CDN
may cache the prior file briefly.
