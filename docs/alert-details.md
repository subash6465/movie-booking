# Optional booking-alert details

Opening alerts retain the movie, theatre/city, show date/time, language/format,
available platforms, booking links and first-detected time. These essential fields
still pass the existing identity, date, preference, stock and freshness guards.

Each platform can additionally supply the following details independently:

| Detail | District | BookMyShow |
| --- | --- | --- |
| Category names | Named, uniquely identified categories | Named, uniquely identified categories |
| Total/available seats | Verified category and show totals only | Omitted: no reliable numeric field established |
| Category availability | Reflected by verified seat counts | Available/unavailable from recognized sellability flags |
| Listed category prices | Positive `areas[].price` | Positive `CurPrice`, without conflicting updates |
| Price range | Category prices shown individually | Verified show range when category prices are absent |
| Auditorium | Explicit session `audi` | Explicit session `ScreenName` |
| Subtitles | Recognized explicit subtitle language | Recognized, nonconflicting subtitle text/acronym |
| Audio | Explicit show-format Dolby Atmos label | Affirmative session Atmos flag, without conflicting event flag |

Details are displayed under their platform with the offer's observation time in
IST. They are **provider-reported snapshots**, not independently verified physical
capacity or guaranteed availability at checkout. Listed prices are not final
checkout totals. No extra seat-map requests, seat holds or paid APIs are used.

## Validation and fallback

- Counts require a complete category list, unique codes, valid names, fixed-seat
  categories, integer values excluding booleans, `0 <= available <= total`,
  agreement with alternate count fields and exact sums matching show totals.
  Contradictory disabled/stock flags suppress numeric enrichment.
- One missing/inconsistent category count suppresses all numeric counts for that
  offer. Independently valid prices, auditorium and subtitles can still appear.
  Missing numbers are never converted to zero.
- BookMyShow availability codes and `BestAvailableSeats` never become counts.
  Unavailable does not necessarily mean sold: seats can be blocked or unreleased.
- Prices reject sentinels, zero, negative, nonfinite and malformed values. Conflicting
  updated prices are omitted; the formatter does not guess which price is current.
- Optional text rejects control characters, placeholders and oversized labels.
  Stored enrichment is whitelisted and validated again before rendering.
- Details need a fresh, timezone-aware observation timestamp. Stale/missing
  details are omitted while the essential alert follows its existing guards.
- Multiple distinct sessions/rooms/prints can share the same deduplication key.
  Optional details are suppressed for that platform when this would make a single
  snapshot ambiguous. Counts are never added across platforms or auditoriums.
- Optional parsing/rendering errors stay in bounded local logs. A valid opening
  message still sends. Optional lines are added only while the complete message
  fits the Telegram length budget, preserving essential content and booking links.

Count or price updates do not trigger another opening alert. Existing baselines,
performance keys, group delivery receipts and cross-platform deduplication remain
unchanged. Existing offers without optional fields remain readable.

Cinema addresses/coordinates, facilities, film runtime/certificate/genre/synopsis,
posters/trailers/cast, ticket quantity limits and booking cutoff times are excluded
from alert enrichment. Conflicting cancellation policies, empty promotions and
unverified seating hints are also omitted. These are not prerequisites for an alert.

See the [field audit](provider-field-audit.md) and
[inventory research](seat-inventory-research.md) for the dated provider observations
that informed these rules. Provider schema/semantics can change; no snapshot or
finite test suite guarantees perfect accuracy or uninterrupted collection.
