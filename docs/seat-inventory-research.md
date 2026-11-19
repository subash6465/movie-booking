# Seat-category inventory research - 19 November 2026

**Implementation update, 19 November 2026:** the optional District snapshot and
BookMyShow category status/price paths are implemented. Counts remain absent from
BookMyShow alerts. See [current validation and fallback rules](alert-details.md).
The sections below preserve the original research conclusions at audit time.

## Decision

The exact requested feature (verified total and available seat counts per category
on both District and BookMyShow, always correct) is **not confirmed**. Numeric
inventory has not been added to alerts. TicketNew removal is independent and is
implemented while preserving existing baseline/delivery history.

District has useful **provider-reported snapshots**. BookMyShow's checked public
showtime data does not provide verified numeric inventory. Never convert its
availability flags or `BestAvailableSeats` into seat counts. No polling system
can promise that a count remains bookable after observation or message delivery.

## Live test and scope

Requests ran from the actual Oracle host, using the public provider routes already
used by the monitor, without credentials, seat selection, seat holds or Telegram
messages. Movie: Avengers: Doomsday, English, Chennai, 17-20 December 2026.
One initial District capture was repeated across the complete four-day window.
The full-window run began at 15:50:59 IST on 19 November 2026.

| Date | Cinemas in response | Shows | Category entries | Arithmetic inconsistencies |
| --- | ---: | ---: | ---: | ---: |
| 17 Dec 2026 | 8 | 32 | 72 | 0 |
| 18 Dec 2026 | 7 | 28 | 64 | 0 |
| 19 Dec 2026 | 7 | 35 | 80 | 0 |
| 20 Dec 2026 | 6 | 30 | 60 | 0 |
| Total | 8 distinct cinemas | 125 | 276 | 0 |

This is finite evidence from one film/city window, not proof for all cinemas,
future provider schemas or physical auditorium inventory.

## District

The live `movies/v5/movie` session objects expose:

- `areas[].label`: category name, such as BUDGET, ECONOMY, PREMIUM or ELITE.
- `areas[].code` and `typeCode`: category identifiers.
- `areas[].sTotal`: provider-reported category total.
- `areas[].sAvail`: provider-reported available count.
- `session.total` and `session.avail`: show-level totals.
- Category/session `statusColor`, seating type and other booking flags.

For all 276 entries, counts were non-negative integers with available <= total.
For all 125 shows, category totals and availability summed exactly to their
corresponding show-level values. This confirms internal consistency of those
responses, not their independent real-world accuracy.

Example from the first capture: PVR Sathyam, 17 December, 13:10 IST:

| Category | District-reported total | District-reported available |
| --- | ---: | ---: |
| BUDGET | 95 | 5 |
| ECONOMY | 312 | 1 |
| PREMIUM | 316 | 1 |
| ELITE | 225 | 1 |
| Total | 948 | 8 |

These were observations at about 15:47 IST on 19 November, not current inventory.
The two public seat-layout HTML pages checked returned HTTP 200 but no embedded
seat inventory in pageProps. The site's published client code requests seat maps
through a separate POST route named `select-seat`. That request was not executed:
its read-only semantics and independent count correctness were not verified.
No seats were selected or reserved. Physical category capacities, provider quotas,
blocked seats and transient holds therefore remain unverified distinctions.

## BookMyShow

Checked raw cinema/show responses for Cinepolis BSR (`CBMC`) and PVR Sathyam
(`PVSR`). Category objects include `PriceDesc`, `PriceCode`, `AreaCatCode`,
`AvailStatus`, `SeatLayout`, price fields and `BestAvailableSeats`.

No verified total-seat or available-seat numeric inventory was present. For an
available BUDGET category at PVR Sathyam, `AvailStatus` was `1` while
`BestAvailableSeats` was `0`. Therefore that field cannot safely be interpreted
as the category's available-seat count. The normal booking page request returned
HTTP 403; an independently usable BookMyShow seat-map path was not verified.

The same Cinepolis show was sold out in BookMyShow's response while District
reported 11 available Executive seats. Counts and availability must be explicitly
platform-specific; never copy District stock into a BookMyShow category or add
counts across platforms for the same performance.

BookMyShow also documents temporary seat blocking during incomplete bookings:
[official support article](https://support.bookmyshow.com/support/solutions/articles/4000159867-transaction-didn-t-go-thru-and-seats-appeared-to-be-blocked).
That supports treating availability as a timestamped observation, not a guarantee.

## Safe implementation boundary, if agreed later

1. Label counts with the platform and a per-offer observation time. Use
   "District-reported total" / "available at check time", not guaranteed cinema
   capacity or reserved seats.
2. Validate category identities, unique category codes, integer types (excluding
   booleans), bounds, totals and all stock/disabled flags. Reject conflicting
   numeric fields and incomplete category lists.
3. Missing or inconsistent numeric inventory is unknown, never zero. Do not infer
   counts from colours, percentages, price bands, `AvailStatus` or best-seat fields.
4. Keep count validation separate from a valid opening alert: omit unverified
   enrichment and log its reason, without fabricating numbers or losing the alert.
5. Expose BookMyShow numeric counts only after a separate seat-map integration is
   verified against the correct movie, cinema, session, screen and show time.
6. Recheck freshness before sending; report that availability can change. Do not
   emit additional alerts merely because the count changed.

Public response captures and consistency reports are retained under ignored
`data/seat-research/` locally and `data/seat-research-20261119/` on Oracle. They
contain no Telegram credentials. No numeric-category implementation or real test
message was sent as part of this research.
