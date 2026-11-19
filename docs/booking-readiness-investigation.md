# Marina Mall listing versus booking readiness

**Implementation update:** the guarded production path is now implemented;
see [current readiness policy](booking-readiness.md). The sections below preserve
the original diagnostic results and the implementation proposal at investigation time.

Investigated **19 November 2026, approximately 20:30–20:36 IST**, from the
production Oracle host. Case supplied by the user: **Avengers: Doomsday,
English, INOX The Marina Mall, Chennai, 17 December 2026**.

This investigation made bounded public listing/map reads. No customer credentials,
seat selection, transaction initialization, seat holds, checkout or Telegram sends
were used. Production monitor code, configuration and notification policy were
not changed. Diagnostic artifacts are ignored under local
`data/readiness-research/live/` and Oracle `data/booking-readiness-20261119/`.

## Confirmed result

At investigation time, the fresh feeds returned one remaining matching Marina
Mall session, **93251 / SCREEN 1 / 16:50 IST**, rather than all the timings
previously visible to the user. These tests reproduce that session only.

| Check | Result |
| --- | --- |
| District movie listing | 462 available / 510 total; green status; no explicit cinema/session disabled flag |
| BookMyShow cinema listing | Same session/time; show status `3`; Executive and Club category status `3`, `SeatLayout=Y`; unpaid flag `N`, cutoff flag `1`, venue-down flag false |
| District seat-page HTML | HTTP 200; this is a JavaScript page shell, not proof of a working seat map |
| BookMyShow booking-page HTML | HTTP 200; likewise not proof of seat-selection readiness |
| BookMyShow seat-layout primary metadata | HTTP 200 with event/venue/show metadata; this alone does not validate the actual layout |
| BookMyShow `GETSEATLAYOUT` for session 93251 | HTTP 200, **`blnSuccess="false"`**, empty map data; explicit cinema connectivity error `(#5)` |
| PVR native `ticketing/seatlayout`, freshly obtained public session token | HTTP 200, **payload status 500**, “Sorry, This Show is no longer available for booking”; no map returned |

BookMyShow's full error was:

> We are facing some connectivity issue with the cinema. Please try again after some time or proceed with another cinema. (#5)

The BookMyShow rejection was reproduced on a second, spaced request. The
application must inspect business success/error fields, not just HTTP status.
The correct film/session/time matched across all three listings, so this is not
evidence of our parser substituting another movie or show time.

## Controls and limits

At the **same Marina Mall cinema**, the current-film control was Bachelor Party
D'eux, 19 November, 22:30 IST, session **93132**:

- PVR returned payload status 200 and a map identifying session 93132 and film
  39402. The map contained 153 named seats, 70 marked available by its `s=1`
  code, after excluding unnamed/padding cells.
- BookMyShow's identical initial map-read request returned
  `blnSuccess="true"`, no exception and nonempty map data for this control.
  Its encoded payload was not independently rendered in a browser.

This rules out a blanket failure of these diagnostic request paths. It does not
prove the cinema is entirely down, identify PVR's internal outage/configuration
reason, or establish that every future Avengers show is failing everywhere.

District's direct `select-seat` map read returned **HTTP 412, non-JSON**, for
both affected Marina Mall and the Cinepolis BSR control. We cannot interpret
that response as “booking closed”: it indicates an unresolved request/access
precondition. No challenge bypass, proxy or repeated identity-changing retry
was attempted. Thus an autonomous District-specific positive map-read check
was **not established** in this investigation.

## What is wrong with the current monitor's evidence

The current monitor validates listing identity, date, preference, fresh observed
stock and disabled flags. It does **not** validate the provider's actual seat-map
operation before treating a new listing as bookable. Reconciled category counts
prove arithmetic consistency of a feed, not successful booking access.

For this exact case, **listed availability and working seat selection disagree**.
The reported cinema connectivity failure is confirmed; whether its underlying
cause is a paused/unreleased session, stale publication, a synchronization problem
or another PVR backend failure is not established by these public responses.
Calling this simply “sold out” or definitely “heavy traffic” would be unsupported.

BookMyShow separately documents [connectivity errors](https://support.bookmyshow.com/support/solutions/articles/4000188331-the-app-website-is-giving-me-a-connectivity-something-is-not-right-here-error-help-).
Its [high-demand sold-out guidance](https://support.bookmyshow.com/support/solutions/articles/4000232934-i-am-getting-sold-out-error-on-seat-selection-page)
and [rate-limit guidance](https://support.bookmyshow.com/support/solutions/articles/4000232930-why-am-i-seeing-a-rate-limit-error-on-the-seat-selection-page-and-what-can-i-do-to-book-my-tickets)
describe other possible failure mechanisms, particularly for event/queue flows;
they are not proof of the internal cause of this movie session's error.

## Implementable next step

Add a separate, provider-specific **booking readiness** stage for new,
non-baseline alert candidates:

1. Retain the existing identity/date/preferences/listing-stock checks.
2. Perform only the provider's initial read-only seat-map operation. Validate
   business success, returned schema and any supplied session/venue identity;
   a positive HTTP status or nonempty HTML page is insufficient.
3. An explicit booking/connectivity error means `NOT_READY`: retain the
   candidate, log locally and retry with bounded backoff. Do not send an opening
   alert or consume its delivery/deduplication receipt.
4. Access denials, HTTP 412/429, timeouts, malformed data, missing identity or
   session/transaction prerequisites mean `UNKNOWN`, not “closed” or “sold out”.
   In strict mode, leave the candidate pending and retry; this sacrifices
   notification coverage while verification is unavailable.
5. After successful readiness verification and a fresh positive stock check,
   record the first verified-ready timestamp and send the normal opening alert
   once. Include only confirmed platform links. Preserve startup suppression,
   cross-platform deduplication and already-sent receipts.

Readiness status must be separate from Telegram delivery state: a failed map
check is not an ambiguous Telegram delivery. Retries must not permanently mark
the performance notified. Count changes or late platform appearance must still
not generate another opening alert.

Run map checks only for pending candidates, with a short bounded cache, pacing,
timeouts and provider-specific retry clocks. All diagnostics remain local; the
bot still sends booking-opening messages only. A successful map read cannot
guarantee seats remain available when the user clicks or checks out.

**BookMyShow:** a direct initial map-read path was demonstrated for both a
successful control and the failing target. It is a public website implementation
detail, not a supported partner API/SLA. Additional representative-cinema tests
and layout/error schema guards are needed before deployment. Some client paths
use `INITTRANS` and `ADDSEATS` to obtain a partial layout; those paths are
outside a passive monitor and were not invoked.

**District:** strict provider-specific readiness requires resolving the legitimate
map-read preconditions or obtaining a supported integration. PVR's native result
can corroborate a problem for an exactly matched PVR session, but must not be
treated as proof of District or BookMyShow's channel-specific seat allotment.
Native PVR availability is never relabelled as either third-party availability.

## Reviewed public client paths

- District's published client distinguishes map reading
  `/gw/consumer/movies/v1/select-seat` from `add-seat`, `set-seat` and checkout.
  Its initial map body supplies cinema/provider/session/movie identity and
  display configuration, with no selected seat IDs.
  [Endpoint/client code](https://cdn.district.in/movies-web/_next/static/chunks/2ziy7l8hgjdr_.js),
  [initial map caller](https://cdn.district.in/movies-web/_next/static/chunks/2rdmthsydag5i.js).
- BookMyShow's published `moviesSeatlayout` module fetches primary metadata
  separately from the `GETSEATLAYOUT` map read. The latter used the published
  `doTrans.aspx` endpoint with transaction identifier 0 and no seat selections.
  [Reviewed client bundle](https://generic-web-assets-in.bmscdn.com/public/build/chunks/js/app.8ab24c4a.js).
- PVR's native read paths and their prior source review are documented in
  [MCP/API research](mcp-api-research.md). Native-chain IDs differ from aggregator
  cinema IDs; Marina Mall was resolved to native cinema **232** from its public
  cinema discovery response, not guessed from its District ID.
