# Provider data contracts and limitations

These adapters use public website responses observed during development. They
are not official partner integrations or stable API contracts. Earlier one-off
research reports were removed during cleanup; their observations are not
permanent reliability guarantees. Automated tests preserve the important rejection
and deduplication rules with controlled payloads.

| Provider | Current collection path | Key checks |
| --- | --- | --- |
| District | City/movie page state for language/format discovery, then `/gw/consumer/movies/v5/movie` | content ID/title, city, variant, actual dates, show/category stock and disabled flags |
| BookMyShow | `/CITY/cinemas` catalogue, then `/api/v3/mobile/showtimes/byvenue` | region, venue, film group/title, discovered child IDs, date consistency, sellable categories/cutoff/session flags |

BookMyShow uses browser-compatible HTTP through curl-cffi, without a browser
process. Catalogue refresh runs independently; cached known cinemas can continue
through catalogue failures. Per-cinema probes read published date hints and then
fetch/validate requested dates. A completely new cinema can be missed while city
discovery is blocked. Cached catalogue data does not establish a fresh negative
baseline. Explicit cache-revalidation headers request fresh data, but do not
prove that the provider's internal database is up to date.

HTTP 401/403/429, stale responses, malformed payloads and identity conflicts are
failures, never evidence that there are no shows. Requests have timeouts, pacing,
size bounds and independent exponential retry clocks (normally capped at 15
minutes). This does not guarantee a provider will allow a cloud IP indefinitely.
There are no proxies, CAPTCHA workarounds or credentialed customer sessions.

Stock checks require positive, consistent availability. A listing can still sell
out before checkout. Platform booking links lead to supported provider movie/show
or seat-layout pages; they do not hold seats and may require user selection/login.
Provider-published exact opening timestamps are not reliably available.

A positive listing is not proof that seat selection will work. In a live Marina
Mall investigation, District reported positive stock and BookMyShow reported an
available session while BookMyShow's actual map read returned a cinema connectivity
error and PVR's map read rejected the same session. The implemented
[readiness policy](booking-readiness.md) requires BookMyShow confirmation for
shared shows, with a user-authorized District-only listing fallback when its
verification is unavailable. See the [reproduction and controls](booking-readiness-investigation.md).

Optional details are extracted from these same responses without new provider
requests. District fixed-seat category counts require exact reconciliation with
show totals; BookMyShow exposes only validated category status/prices. Partial or
untrusted enrichment is omitted independently of the opening alert. See
[alert detail contracts](alert-details.md) for field validation, ambiguity and
message-length handling.

Deduplication needs correct theatre identity. Different provider names require
verified aliases. Matching identical names is not a universal venue registry.
No fuzzy identity guess or sold-out-to-available baseline reopening alert is used.

Tests cannot prove 100% completeness or uninterrupted live access. If checks fail,
all diagnostics stay in local logs; the Telegram group receives booking alerts
only. No source cleanup changes these external constraints.
