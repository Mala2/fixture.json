# Soccer fixture updater

The updater queries TheSportsDB, validates the provider response, normalizes
one usable event into the existing ESP32 schema, and atomically replaces
`fixture.json` only when meaningful fixture data changed. It uses only the
Python standard library.

## Active provider configuration

The active and default provider is `thesportsdb`:

```text
FIXTURE_PROVIDER=thesportsdb
THESPORTSDB_API_KEY=123
THESPORTSDB_TEAM_ID=136013
FIXTURE_OUTPUT_PATH=fixture.json
FIXTURE_REFRESH_AFTER_SECONDS=21600
TARGET_TEAM_SLUG=al-hilal
TARGET_TEAM_SHORT_NAME=HIL
```

TheSportsDB key `123` is its public free key, so no private provider secret is
required. Team ID `136013` is used only inside the updater to query and
identify Al Hilal. The device-facing normalized ID is always `al-hilal`.

The scheduled request is:

```text
GET https://www.thesportsdb.com/api/v1/json/123/eventsnext.php?id=136013
```

The updater uses the known team ID directly because the free team-search
endpoint is unreliable or restricted for arbitrary searches. It does not call
team search or team lookup during scheduled updates.

For a manual identity diagnostic only:

```sh
curl --fail --show-error --silent \
  'https://www.thesportsdb.com/api/v1/json/123/lookupteam.php?id=136013'
```

The lookup validator accepts controlled Al Hilal name variants, requires
`idTeam=136013`, and requires `strSport=Soccer` when sport is supplied.

## Normalized identity policy

Normalized IDs belong to the device contract and are provider-independent.
Provider IDs are transport details internal to the updater.

- Root `team.id` is `al-hilal`.
- The fixture-side Al Hilal ID is `al-hilal`, whether Al Hilal is home or
  away.
- Provider target ID `136013` is never emitted as Al Hilal's normalized ID.
- Opponent TheSportsDB IDs are preserved as strings when present and valid for
  the existing firmware schema.
- If an opponent ID is absent, a deterministic name slug is used.

Controlled provider names `Al-Hilal`, `Al Hilal`, `Al-Hilal Saudi FC`, and
`Al Hilal SFC` normalize to name `Al Hilal`, short name `HIL`, and ID
`al-hilal`.

Team abbreviations remain in `team_aliases.json`. It includes `Al-Hilal`,
`Al Hilal`, and `Al-Ahli Doha`; unknown opponents use the deterministic
fallback generator.

## Provider validation and mapping

The client uses HTTPS, a finite timeout, HTTP-status checks, a 1 MiB response
limit, strict JSON parsing, and an `events` array requirement. Normal
scheduled logs contain counts and selected fields, never the complete provider
response.

Provider fields map as follows:

| TheSportsDB | Normalized field |
| --- | --- |
| `idEvent` | `fixture.id`, prefixed with `thesportsdb-` |
| `strLeague` | `fixture.competition` |
| `strTimestamp` | `fixture.kickoff_utc` |
| `strVenue` | `fixture.venue` |
| home/away IDs and names | normalized home/away team objects |
| `strStatus` | `fixture.status` |

`idHomeTeam` and `idAwayTeam` are compared with internal target ID `136013`
to determine `home_away`. An event is rejected if the target is absent or
ambiguous, its event ID or names are missing, kickoff is invalid, competition
is missing, status is unusable, or it is clearly finished/in the past.

Missing venue uses `Venue TBC`, which is accepted by the unchanged ESP32
schema. An empty `events` array is a valid no-fixture result: the updater exits
without replacing the previous file.

## Timestamp policy

`strTimestamp` is parsed first. It accepts ISO timestamps to minutes or
seconds, with or without an explicit offset.

- Explicit offsets are converted to UTC.
- A timestamp without an offset follows TheSportsDB's UTC convention. The
  updater logs a warning and emits `Z`.
- `dateEvent` and `strTime`, when present, must agree with the provider
  timestamp's source date and time.
- Invalid, impossible, or conflicting timestamps fail safely.

Provider timestamps are never interpreted as `America/Los_Angeles`; local
conversion remains the ESP32's responsibility.

## Status policy

| TheSportsDB status | Device status |
| --- | --- |
| `NS`, `Not Started`, `TBD` | `scheduled` |
| `POSTP`, `Postponed` | `postponed` |
| `CANC`, `Cancelled` | `cancelled` |
| `FT`, `Match Finished` | `finished` |
| recognized live/in-progress values | `live` |

An empty status with a future kickoff is inferred as `scheduled` with a
warning. An unknown non-empty status is never silently treated as scheduled.

## Safety and free-tier limitation

The normalized document is strictly validated, serialized as UTF-8 with
two-space indentation and a trailing newline, written through a temporary
file, and atomically replaced. Validation and provider failures preserve the
last valid `fixture.json`. A generated-time-only difference does not rewrite
the file.

The free `eventsnext.php` endpoint may return only one upcoming home event.
The result therefore must not be described as a guaranteed absolute next
home-or-away fixture. This limitation is accepted for the prototype.

The provider boundary is `provider_common.ProviderFixture`. To change
providers later, add a bounded client/parser that produces that neutral model,
then explicitly change the provider selection and workflow configuration.
Keep provider identity mapping in the normalization layer and do not add a
silent fallback provider.

## Run locally

Run all updater tests:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s tools/fixture_updater/tests -p 'test_*.py' -v
```

Validate with the saved provider sample without changing `fixture.json`:

```sh
FIXTURE_PROVIDER=thesportsdb \
THESPORTSDB_API_KEY=123 \
THESPORTSDB_TEAM_ID=136013 \
python3 tools/fixture_updater/update_fixture.py \
  --dry-run \
  --provider-sample \
  tools/fixture_updater/tests/fixtures/thesportsdb_next_event.json
```

Run against the live endpoint without changing `fixture.json`:

```sh
FIXTURE_PROVIDER=thesportsdb \
THESPORTSDB_API_KEY=123 \
THESPORTSDB_TEAM_ID=136013 \
python3 tools/fixture_updater/update_fixture.py --dry-run
```

Exit codes are `0` for success, `2` for configuration, `3` for no usable
upcoming event, `4` for provider transport/response validation, and `5` for
normalization/output validation.

GitHub Actions runs the tests first, refreshes every six hours or on manual
dispatch, and commits only a meaningful `fixture.json` change.
