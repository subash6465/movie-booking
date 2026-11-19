# District and BookMyShow: exposed data versus current usage

**Implementation update, 19 November 2026:** optional booking details are now
implemented under the stricter [alert detail contracts](alert-details.md). The
usage tables below record the pre-enrichment audit. Excluded movie/venue metadata
and unverified policies/promotions are not included in alerts.

Checked on **19 November 2026**, with fresh read-only requests from the Oracle
host beginning at **16:37:51 IST**. Movie: Avengers: Doomsday, English, Chennai,
requested show date 17 December 2026. No production code/configuration changed,
no seat selection or reservation was made, and no Telegram message was sent.

## Seat-data conclusion

**District directly exposes numeric show/category inventory in the showtime feed
we already call.** We can support timestamped, platform-reported seat counts
without adding an MCP server or fetching a seat map for those counts.

BookMyShow's checked cinema/showtime feed directly exposes category names,
listed prices and availability states. A reliable numeric total/available count
per category has not been established from that feed. A bounded read-only check
of the legacy `GETSHOWINFO` route also returned HTTP 403. That is a failed route
test, not proof that all possible BookMyShow seat-map access is impossible.

Neither platform's snapshot guarantees that seats remain bookable at delivery or
checkout time. Neither showtime feed provides verified exact seat positions or
adjacency in the responses checked here. Those require separately validated
seat-map access. The exact booking-opening timestamp is also not supplied.

## Current implementation

`Show` stores provider/movie/cinema/session identity, start time, language,
format, availability, booking URL, screen and an optional overall available-seat
count. `State` persists those per-offer records. The opening message currently
shows movie, cinema/city, date/time, language/format, available platforms, links
and first detected bookable time.

District's `avail` is stored as `available_seats`, but is not rendered in the
message. Both parsers store auditorium/screen names, also not rendered. Category
counts/statuses are partly read for availability validation; the category labels,
totals and prices are discarded rather than retained as structured inventory.

Source: [domain model](../movie_monitor/core.py),
[District parser](../movie_monitor/providers/district.py),
[BookMyShow parser](../movie_monitor/bms.py),
[message formatter](../movie_monitor/delivery.py).

## District fields we could use further

| Information | Observed fields | What the application currently does |
| --- | --- | --- |
| Category names and IDs | `areas[].label`, `code`, `typeCode` | Does not retain/render category identities |
| Seats per category | `areas[].sTotal`, `sAvail` | Reads availability counts for stock validation; does not retain/render category totals/counts |
| Overall show inventory | session `total`, `avail` | Stores available count; ignores total; neither is displayed |
| Listed category price | `areas[].price` | Not retained, displayed or used as a price preference |
| Auditorium | `audi` | Stored as `screen`, not shown in the alert |
| Subtitles | `subtitle` | Not used |
| Ticket quantity limit | `maxTkt` | Not used; no multi-ticket booking feature currently exists |
| Category seating type | `areas[].sType` | Not exposed to users; would need explicit handling for fixed/free seating |
| Venue address/location | `cinemaInfo.address`, `pincode`, `latitude`, `longitude`, city/state | Only the cinema name/ID is used; venue location is not used for distance preferences or map links |
| Chain/group identity | `chainKey`, `theaterGroupName`, provider/venue IDs | Not used as a chain preference; can help reviewed venue identity mapping |
| Reported distance | `distanceFromUser` | Not used; it is relative to coordinates sent in the request, not necessarily the recipient's home |
| Cinema facilities | Cinema `amenities` IDs plus `meta.amenities` names | Not used; only IDs attached to that cinema describe its facilities |
| Mobile tickets | `cinemaInfo.mTicket` | Not displayed or filterable |
| Cancellation/modification flags | `isMerchantCancellable`, `isModifiable` | Not displayed; do not infer a refund amount or unconditional eligibility |
| Film metadata | release date, duration, censor, genre, synopsis, languages, cast | Uses title/ID/language for validation; other metadata not used |
| Media | poster URLs, horizontal images, trailers/videos | Not used |
| Ratings/reviews | `ratings`, `reviews` | Not used; both were empty for this sampled film |
| Published dates | `meta.showDates` | Not used for discovery; the monitor requests configured dates explicitly |
| Offers/promotion fields | `offerTags`, `cinemaMeta.promos`, related banners/flags | Not used; no populated venue-offer examples were found in this response |
| Provider response time | root `serverTime` | Not used; could be an additional response-time sanity check, not proof of inventory refresh/opening time |

The fresh District response contained eight cinemas and 32 show sessions.
At Cinepolis BSR, its first session's two category entries were:

| Category | Provider-reported total | Provider-reported available | Listed category price |
| --- | ---: | ---: | ---: |
| NORMAL | 17 | 0 | INR 61.74 |
| EXECUTIVE | 163 | 11 | INR 210.98 |

These are **observations at check time**, not current inventory or guaranteed
physical auditorium capacity. Prices are provider-listed ticket values; final
checkout charges, add-ons and offers were not verified.

The sampled cinema's actual amenity IDs resolved to Food & Beverages, Parking,
Digital Payments, Mobile Ticket and Air Conditioning. PVR Sathyam's attached
IDs additionally included Wheelchair Friendly. The global amenities dictionary
also contained other facilities, but that does not mean every cinema has them.

## BookMyShow fields we could use further

| Information | Observed fields | What the application currently does |
| --- | --- | --- |
| Category names and IDs | `Categories[].PriceDesc`, `PriceCode`, `AreaCatCode` | Not retained/rendered |
| Category listed prices | `CurPrice`, optional `UpdatedPrice` | Not used |
| Category availability state | `Categories[].AvailStatus`, `SeatLayout` | Used for the overall bookability guard; not displayed per category |
| Show price range | `MinPrice`, `MaxPrice`, optional updated values | Not used |
| Screen/auditorium | `ScreenName` | Stored, not rendered |
| Subtitles | `SessionSubTitle`, `SessionSubTitleAcronym` | Not used |
| Additional audio flags | `IsAtmosEnabled`, `EventIsAtmosEnabled` | Not normalized separately; existing format handling already reads dimension/attributes |
| Exact cutoff metadata | `CutOffDateTime` | Not retained/rendered; the existing guard uses `CutOffFlag` |
| Address/location | `Venues.VenueAdd`, `VenueLat`, `VenueLong` | Not used for venue distance, navigation links or identity confirmation |
| Chain and venue type | `VenueCompName`, `VenueComp`, `IsMultiplex` | Not used as preferences |
| Ticket/food facilities | `MTicket`, `ETicket`, `IsFoodSales`, venue legends | Not displayed; opaque legend codes need a verified mapping |
| Cancellation/policies | `TicketCancellation`, `VenueInfoMessage`, policy text | Not displayed; show-specific conditions and provider checkout remain authoritative |
| Current response venue health | `isVenueDown`, related venue flags/messages | Not fully consumed from the show response; the existing guard uses cached catalogue venue health and certain session flags |
| Film metadata/media | duration, genre, censor, trailer URL, image code, synopsis fields | Identity/language/format are used; other fields not used |
| Offers | show `Offers`, date-level `BMSOffers` | Not used; sampled values were null |
| Child/couple/bestseller hints | `ChildSeats`, `CoupleSeats`, `BestSeatsAvail`, `BestAvailableSeats` | Not used as seat counts or guaranteed seating features; semantics/numeric accuracy not verified |

The fresh PVR Sathyam example contained named Budget, Economy, Premium and Elite
categories and their listed prices. Budget had a positive availability state
while `BestAvailableSeats` was zero. That field is therefore not a safe numeric
count for the category. No verified total/category count fields were found.

## Important interpretation limits

- The fresh responses reported different runtimes for this film: District 150
  minutes and BookMyShow 155 minutes. Metadata should retain its source rather
  than be treated as independently verified fact.
- District's `minTicketPrice` was `-1`, despite valid positive prices in category
  entries. That sentinel must not be shown as a negative ticket price.
- An empty reviews/ratings array is unknown, not a zero-star rating. Null offers
  do not establish a complete list of promotions.
- Availability colours/codes are not numeric counts. Missing counts are unknown,
  not zero. Use explicit valid zero values only when the provider supplies them.
- Generic warning text and UI templates can appear in otherwise healthy replies.
  A message such as `DownCinemasMessage` is not itself proof that the venue is down;
  evaluate the corresponding live flags.
- Cancellation flags can coexist with generic policy text that says tickets are
  non-cancellable. We cannot promise cancellation/refund conditions from one flag.
- Fixed/free seating and accessibility restrictions require their own handling.
  Overall free-seat counts cannot prove that several seats are adjacent.

## Recommended enrichment order

1. District category names, total/available snapshots and listed prices, with a
   per-platform check time and omission of invalid/missing figures.
2. BookMyShow category names, listed prices and availability states; keep numeric
   counts absent until independently verified.
3. Show auditorium and subtitles in compact opening messages. These do not need
   additional provider requests when the fields are already present.
4. Add venue address/navigation and optional price/category preferences in a
   later configuration change, preserving the current baseline/delivery policy.
5. Keep posters, cast, trailers, facilities and detailed policy information in
   the future app UI rather than making Telegram opening alerts excessively long.

This does not require adopting a new MCP for the fields already in the current
HTTP responses. It is an audit/recommendation only; alert formatting and running
code were not changed.

Fresh public samples and the complete observed key inventory are retained under
ignored local `data/field-audit/` and Oracle `data/provider-field-audit-20261119/`.
