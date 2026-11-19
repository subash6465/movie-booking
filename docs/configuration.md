# Configuration

`watches.example.json` is the single supported example. Copy it to `config.json`
for a new installation and validate with `python -m movie_monitor --validate`.
Unknown fields are rejected so spelling mistakes cannot silently widen a watch.
The private file is not packaged or committed.

## Global options

| Field | Default | Accepted values |
| --- | --- | --- |
| `version` | 2 | 2 |
| `watches` | required | 1-5 movie objects, each with a unique ID |
| `poll_seconds` | 60 | 60-3600 |
| `aggregation_seconds` | 0 | 0-60; compatibility only, guarded delivery never waits for aggregation |
| `readiness_retry_seconds` | 2 | 1-60; pending-map business rejection retry interval |
| `readiness_timeout_seconds` | 8 | 2-20; per-request network timeout |
| `readiness_freshness_seconds` | 10 | 5-30; maximum lifetime of an accepted map proof |
| `freshness_seconds` | 180 | 30-600 |
| `max_backoff_seconds` | 900 | 60-3600; each schedule has its own retry clock |
| `catalogue_refresh_seconds` | 60 | 60-600 |
| `bms_background_checks` | 2 | 1-20 cinema probes per batch |
| `bms_probe_interval_seconds` | 5 | 5-120 between batches during stale discovery |
| `observation_retention_days` | 7 | 1-90 |
| `theatre_aliases` | [] | Verified mappings described below |

A cinema/date normally uses its watch's polling interval. Discovery uses an
independent worker and retry clock. HTTP responses with `Age` above 180 seconds
are always rejected, even if `freshness_seconds` is set higher; the latter
controls application observation freshness. Missing `Age` does not prove that
the provider's underlying data is current.

Readiness map requests reject an explicit HTTP cache Age above 5 seconds.
Scheduling options do not change preference generations or reset baselines.
See [readiness](booking-readiness.md) for denial backoff and current platform coverage.

## Each watch

| Field | Meaning |
| --- | --- |
| `id` | Unique lowercase letters/digits/hyphens, at most 64 characters |
| `enabled` | Optional boolean; defaults to true |
| `movie` | Exact verified title; punctuation normalization is allowed |
| `city` | Provider city slug, e.g. `chennai` |
| `date_range` | Inclusive `{ "from": "2026-12-17", "to": "2026-12-20" }`, at most 14 days |
| `dates` | Alternative to `date_range`; ISO dates or `today`, `tomorrow`, `+2d` |
| `time_windows` | Map dates or `*` to lists of inclusive start/end pairs |
| `languages` | Allowed language names; [] means any |
| `formats` | Allowed labels, e.g. IMAX, EPIQ, NORMAL, 2D or 3D; [] means any |
| `theatres` | Provider IDs, names or configured alias identities; [] means any |
| `providers` | At least one of `district`, `bookmyshow` |
| `poll_seconds` | Optional watch-specific override, 60-3600 |
| `coordinates` | Optional latitude/longitude for District in an otherwise unmapped city |

Time windows use show-start times in IST (Asia/Kolkata). For example:

```json
"time_windows": {
  "*": [["18:00", "22:00"]],
  "2026-12-19": [["10:00", "12:00"], ["18:00", "23:00"]]
}
```

A date-specific entry replaces the `*` windows for that date. Split overnight
windows across calendar dates. Use explicit dates for release-specific watches;
relative dates move as the calendar changes and should be chosen deliberately.

District needs `content_id` and can include `slug`.
BookMyShow needs a verified `event_group` (EG...) and `region_code` (e.g. CHEN),
plus optional `slug` and `event_codes` diagnostic metadata. Child event IDs are
discovered from cinema responses, not restricted to that metadata list. Never
guess an ID: validate the exact movie/city association first.

## Theatre aliases

Providers lack a universal theatre ID. The example maps Cinepolis BSR OMR:
BookMyShow `CBMC`, District `9505`. Each alias has a unique `id`, `city`,
`name`, `providers` mapping to lists of theatre IDs, and optional alternative
`names`. Add only verified equivalents. Aliases migrate existing performance
identities and sent history; they do not replay already sent alerts.

Exact normalized names are fallback links.
There is no fuzzy merge. Review `state.name_based_theatres` in status for venues
that may need aliases. Distinct same-provider IDs with an ambiguous name are
rejected until an explicit mapping resolves them.

The example also includes seven reviewed PVR/INOX mappings, including Marina Mall.
These map identities and do not restrict `theatres: []`. Startup/config reload
reconciles stored offers and receipt identities before applying the shared-show
BookMyShow veto or District-only fallback. See [readiness policy](booking-readiness.md).

## Reload and baseline rules

The file is checked about every five seconds. Invalid JSON pauses new deliveries
and preserves the last valid collector configuration. Changes to preferences
create a new silent baseline. Watch ID, enable flag, polling interval, date-array
ordering and BookMyShow child-event metadata do not create a new preference
generation. Disabling a watch stops its new collection/delivery work. Telegram
credentials are loaded at startup; restart the main service after changing `.env`.
