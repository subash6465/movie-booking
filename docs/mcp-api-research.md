# Cinema APIs and MCPs: implementation-focused research

Research date: **19 November 2026**. Scope: India, especially Chennai,
booking-opening alerts, category-level seat counts, future seat preferences,
and the existing zero-cost Oracle/Python/SQLite/Telegram monitor.

## Decision

**PVR's native web API is a credible candidate for a separate PVR/INOX adapter.**
Live read-only requests from our actual Oracle host returned cinema sessions and
a seat map from which category counts could be calculated. This is a new data
source, rather than a way to make BookMyShow's own inventory available.

**District's provider-owned MCP is interesting for personal experiments and the
future booking phase, but cannot currently be adopted as the backend of our
third-party application without District's approval.** Its published manifest
explicitly restricts that use. An authenticated client is required even though
the sign-in flow supports guest browsing.

**Do not replace our monitor with an MCP watcher or generic scraping wrapper.**
Keep the existing baseline, identity, freshness and durable-outbox policy. MCP
is an interface to a data source; it does not guarantee freshness, complete
coverage, background execution, a free service or permission to redistribute data.
Our deterministic worker can call a suitable interface without an LLM in the
polling loop. [MCP architecture](https://modelcontextprotocol.io/docs/learn/architecture)

This task changed documentation/research artifacts only. No provider adapter was
added, no production configuration was changed, no service was restarted, no
seats were selected/held and no Telegram message was sent.

## What was actually checked

Three kinds of evidence are kept separate:

1. **Source review:** repository code, licences, models, error paths and transport
   behavior. Readmes alone were not treated as reliability evidence.
2. **Live checks from Oracle:** bounded provider requests, MCP initialization,
   tool discovery, narrow showtime lookups and two PVR seat-layout requests.
3. **Documentation only:** services needing account/API-key access or provider
   permission. Their advertised features are not represented as live-verified.

Public responses are under ignored `data/mcp-research/live/`; reviewed source
snapshots are under ignored `data/mcp-research/source/`. No Telegram credentials
or production database copies are included.

## 1. pvr-inox-mcp

### Identity, source and licence

The original directory listings point to
[`notprashanth/pvr-inox-mcp`](https://github.com/notprashanth/pvr-inox-mcp), which
returned 404 in both public GitHub and connector checks. That means it was not
publicly retrievable during this research; it does not establish why.

I reviewed the accessible
[`syedxx28-lgtm/pvr-inox-mcp`](https://github.com/syedxx28-lgtm/pvr-inox-mcp)
copy and its MIT licence, which credits Prashanth Krishnan. Reuse of its code
requires retaining the applicable licence/copyright notice. The licence covers
software; it is not a contract granting PVR API access, a data licence or an SLA.
[Licence](https://github.com/syedxx28-lgtm/pvr-inox-mcp/blob/main/LICENSE)

Reviewed file blobs include `core.py` `ef497ed6f8fd0e9c8ed0ee7b5d2191cf6cd80076`,
`mcp_server.py` `1499621d5b2dd497a67b950c686a7f5bc6a3b864`, and
`watch.py` `4e758377c1f180763be877f3791f637c6b9d1bd5`.
The latest inspected branch commit was a generated state update, not evidence
of a new reviewed software release.

### What it supplies

The API client and optional MCP server expose PVR/INOX cinema/show discovery,
seat maps, seat-zone analysis, adjacent-seat runs and direct show links. The
separate watcher supports opening and restock-style events with several delivery
channels. Its polling/storage/notification behavior differs from our application's
policy. Its core uses Python's standard library; running its MCP server also
requires an MCP SDK dependency. [Core](https://github.com/syedxx28-lgtm/pvr-inox-mcp/blob/main/core.py),
[watcher](https://github.com/syedxx28-lgtm/pvr-inox-mcp/blob/main/watch.py),
[dependencies](https://github.com/syedxx28-lgtm/pvr-inox-mcp/blob/main/requirements.txt)

Its coverage is the **PVR/INOX chain**, not Cinepolis or all independent theatres.
The advertised city coverage was not individually tested. Native-chain availability
must never be labelled as BookMyShow or District availability.

### Data path and live results

Reviewed read requests use JSON POSTs beneath
`https://api3.pvrcinemas.com/api/v1/booking`:

| Path | Purpose | Observed result |
| --- | --- | --- |
| `content/cinemas` | Cinema discovery around a location | HTTP 200, but no cinema entries for the first coordinate query; one query cannot prove city-wide coverage |
| `content/csessions` | One cinema's sessions for an explicit date | HTTP 200 with session blocks for Palazzo on 19 Nov and 17 Dec |
| `ticketing/seatlayout` | Read a session's seat map using its public booking token | Successful current-film map; Avengers case returned an explicit unavailable response |

The guest request headers described by the client, including a blank bearer
value, worked for these direct session/map checks. No customer account token,
login, reservation call, payment call or private proxy was used.

At **16:09:36 IST**, one live test returned:

- Cinema: PVR Palazzo-The Nexus Vijaya Mall, native cinema ID `388`.
- Film: **Bachelor Party Deux**, Malayalam, 19 November, 19:00 IST.
- Session `41295`, print/film ID `39402`; the returned layout identified that
  same show/film/cinema/time.
- Categories derived from the map's area headers and named seat cells:

| Category | Named seats in returned map | Cells marked available (`s=1`) |
| --- | ---: | ---: |
| Budget | 27 | 2 |
| Elite | 210 | 53 |
| Total | 237 | 55 |

There were no duplicate row/seat labels in that sample. Aisle/padding cells with
no seat name were excluded. These are a timestamped provider-map observation,
not verified physical fire-code capacity or a promise that checkout will accept
the seats later. Availability-code interpretation agrees with the reviewed client;
no seat was clicked to test a booking.

The **Avengers: Doomsday** test was a different outcome: 17 December, 13:35 IST,
Palazzo AUDI 8, session `41291`. Sessions were listed, but the layout request
returned HTTP 200 with payload status `500` and the message that seats were no
longer available. No category counts could be established for that case. An
unavailable response must not be converted into invented zero totals. Thus this
research does **not** prove that native PVR can supply the requested Avengers
counts whenever District/BookMyShow shows the performance.

### Hosted MCP: working, but not our production dependency

The publicly advertised community endpoint
`https://pvr-inox-mcp-612942167838.asia-south1.run.app/mcp` accepted initialization
and read-only calls without customer credentials. The live tool list contained:

`pvr_cities`, `pvr_cinemas`, `pvr_now_showing`, `pvr_showtimes`, `pvr_seats`,
`pvr_seat_layout`, `pvr_find_shows`, `pvr_screens`, `pvr_is_open`,
`pvr_compose_watch`.

The last tool composes configuration; it is not evidence that a persistent
background watch has started. We successfully queried Palazzo discovery and
Avengers showtimes. Tool-list and showtime success do not prove long-term uptime
or numeric seat correctness for every show. The public service's tool set also
differs from the older readme's smaller remote-tool list: integrations must pin
and validate schemas rather than assume directory descriptions are current.

For our service, a small direct adapter is preferable to depending on another
developer's shared hosted instance. There is no verified free-forever service
commitment, throughput agreement or uptime guarantee. Native API requests were
not billed in this test, but that is not a future commercial access commitment.

### Code that should not be copied unchanged

- `day_sessions` classifies a broad range of non-success payloads as a closed
  date. A generic server error is not reliable negative inventory evidence.
- The watcher can issue `back_in_stock`/`seats_freed` notifications. Our baseline
  policy intentionally does not alert when an initially present show reopens.
- Its title-substring matching is insufficient for our exact film/variant guards.
- `core.py` changes the process-wide DNS resolver when imported. That should not
  be introduced accidentally into our existing service.
- Claims that an unobserved-free seat was never sold are inferences. Observation
  history cannot prove what happened before startup or between polls. Report
  unavailable/unknown rather than inventing sold-versus-withheld classifications.
- One map per show adds load. Cache discovery, share schedule requests across
  movie watches and limit enrichment to fresh candidate alerts or explicit
  seat-preference watches. Respect provider-wide denial/cooldown behavior.

These are findings from the reviewed implementations, not a claim that the
repositories are malicious. [Core implementation](https://github.com/syedxx28-lgtm/pvr-inox-mcp/blob/main/core.py),
[watch implementation](https://github.com/syedxx28-lgtm/pvr-inox-mcp/blob/main/watch.py)

## 2. District's official MCP

This is a provider-owned connector at `https://mcp-server.district.in/v2/mcp`.
Its public description includes show discovery, prices, remaining seats,
cinema details and a booking flow using a live seat map. The manifest separates
read operations (`search`, `execute`) from mutations (`act`) and payments (`pay`).
[Official site](https://studio.district.in/mcp/),
[official manifest](https://github.com/Eternal-District/mcp-server-manifest)

**Live access result:** an unauthenticated initialization returned HTTP 401,
`no bearer token`. OAuth metadata advertised authorization-code/refresh-token
flows and PKCE. Guest browsing still uses that sign-in/authorization flow; it
does not mean anonymous requests to the endpoint are accepted. No account/guest
session was created during research, and authenticated seat payloads were not
tested.

**Adoption restriction:** the pinned official manifest
`fca8162c6e487a2375dc945ef2d76060ad2b8881` says third-party apps are not currently
allowed on top of District MCP and directs product builders to District. It
also restricts OAuth redirect URIs to listed clients and describes the connector
as testing/personal-use functionality.
[Manifest disclaimer and setup](https://github.com/Eternal-District/mcp-server-manifest/blob/fca8162c6e487a2375dc945ef2d76060ad2b8881/README.md)

Consequently: personal evaluation is possible through supported clients; use as
our unattended multi-user application's backend is **not approved or verified**.
Provider permission, our redirect/client support, unattended token lifecycle,
polling limits, redistribution terms and costs require clarification first.
No contact message was sent on the user's behalf.

For phase two, this is a genuine provider-described route toward seat holds and
user-approved UPI payment. It is not a permission-free reservation API for our
bot. The current monitor should never receive booking/payment tools or shared
customer credentials merely to read showtimes.

## 3. pvr-inox-radar

[`karanb192/pvr-inox-radar`](https://github.com/karanb192/pvr-inox-radar) is a
related MIT project/agent skill, not a replacement indefinitely running monitor.
Reviewed commit: `685631ad438a27d378d0857624c8f146c49bd912`.

Its useful design ideas are bounded/sequential native PVR requests, shortlist-only
seat maps, aisle-aware adjacency, price-tier/recliner filters, coordinate caching
and a map-based display. It has a substantial offline fixture/test tree. Those
tests were inspected, not executed or treated as our live service validation.
Its current design explicitly avoids continuous polling. Geocoding/maps also
bring their own service policies and connectivity dependencies.

Good reference for a future preference such as “three adjacent seats in the
centre section under a price limit.” Reuse small reviewed parsing/geometry
components with attribution if selected; do not install the entire skill as a
background booking bot. [Client and notes](https://github.com/karanb192/pvr-inox-radar/tree/main/skills/pvr-inox-radar)

## 4. BookMyShow MCP webscraper

[`m0han-r/bookmyshow-mcp-webscraper`](https://github.com/m0han-r/bookmyshow-mcp-webscraper)
provides REST/MCP/CLI interfaces for listings, movie details, venues, prices and
availability labels. Reviewed commit: `848707a12c2679d9414fb6184b64bd9f5df57c7d`.

Its model has an availability string, not numeric inventory per category. The
parser defaults missing availability to AVAILABLE, and collection paths can
return empty lists after exceptions. The configured cache TTL is five minutes.
Those semantics are unsuitable for our conservative opening/baseline rules.
It uses normal website requests, so the MCP layer does not remove upstream
blocking. [Models](https://github.com/m0han-r/bookmyshow-mcp-webscraper/blob/main/bms_scraper/models.py),
[parser](https://github.com/m0han-r/bookmyshow-mcp-webscraper/blob/main/bms_scraper/parser.py),
[client](https://github.com/m0han-r/bookmyshow-mcp-webscraper/blob/main/bms_scraper/scraper.py)

Verdict: useful as discovery/UI reference; not a verified improvement to our
collector or a solution to BookMyShow numeric seat counts. Package metadata
declares MIT, but the inspected tree had no separate LICENSE file; clarify the
licence text before substantial code reuse. No package was installed or run.

## 5. Other relevant APIs

| Service | What it can add | Zero-cost fit and verdict |
| --- | --- | --- |
| [TMDB API](https://developer.themoviedb.org/docs/faq) | Movie search, posters, cast, synopsis and regional release metadata for the app UI | Free non-commercial use with attribution; commercial use needs its own licensing. It is not a source of live theatre seat maps or booking-opening events. Useful optional metadata layer. |
| [MovieGlu](https://movieglu.com/contact-general/) | Normalized showtimes and cinema booking links | Evaluation access is limited; ongoing live production access is priced. Exact Indian coverage and update latency need evaluation. It explicitly does not support seat selection/payment. Not a zero-cost core replacement. |
| [International Showtimes REST/MCP](https://www.internationalshowtimes.com/agentic-showtimes-api) | Cross-cinema discovery, formats/languages and booking links | REST uses API keys/trial access; the agentic MCP page advertises early access/coming-soon availability. No freely usable live MCP or Indian category inventory was verified. Commercial discussion, not a free drop-in. |
| [AmoDB showtimes](https://amodb.dev/showtimes/) | Movie-to-cinema showtimes and booking links | Published API pricing is paid; showtime docs specify 30-minute edge caching. Indian completeness and category inventory not verified. Poor fit for rapid opening alerts. |
| [CinemaIP Data API](https://cinemaip.ai/data-api) | Indian film metadata and theatre/chain occupancy or box-office analytics | A free plan/key is advertised, but production quotas were not verified. Data is hourly; this does not establish per-seat/category availability or immediate opening detection. Optional future analytics, not inventory authority. |
| [Gracenote/TMS](https://developer.tmsapi.com/docs/read/data_v1_1/movies) | Licensed movie/theatre/showtime data | The reviewed Movies API documents US/Canadian coverage. Do not assume it covers Chennai or is a free Indian inventory source. Not a current fit. |
| [OSM Nominatim](https://operations.osmfoundation.org/policies/nominatim/) | Turn a user-entered locality into coordinates for distance preferences | Optional only after an informed policy decision: identifying requests, attribution, caching, absolute 1 request/second across the app, no autocomplete/systematic searches; regular scripts have stricter limits. Never call on every movie poll. It supplies locations, not showtimes/seats. |

For MovieGlu, the evaluated documentation distinguishes limited real-data trials
from a sandbox. Sandbox/sample data is not production inventory. Their pages
also differ on sandbox request limits, so no unlimited free allowance is assumed.
[Developer quickstart](https://developer.movieglu.com/v2/api-index/quick-start-guide/),
[pricing](https://movieglu.com/pricing/)

MovieGlu's documented cinema day runs 03:00-02:59, unlike our user's calendar-date
preferences. Any adapter would need explicit after-midnight normalization.
[Date conventions](https://developer.movieglu.com/v2/api-index/filmshowtimes/)

## Feature impact for our application

| Requested/future feature | Most relevant source | Assessment |
| --- | --- | --- |
| New matching shows at any theatre | Existing District/BookMyShow, possibly PVR native added separately | Keep both current providers; PVR covers only its own chain and can have different inventory/windows |
| Category names, total mapped seats and currently available seats | District's existing category fields; native PVR maps for PVR shows | District snapshots already researched; PVR map counting now has a live proof of feasibility, not full production validation |
| Two/four seats together | Native PVR seat cells + aisle-aware geometry | Feasible candidate; category totals alone cannot answer adjacency |
| Preferred section/recliner/price | PVR maps/price tiers, radar's algorithms | Possible where provider fields support it; needs per-cinema validation and explicit user policy |
| Better booking handoff | Native PVR's per-show link; current provider links | Useful; no seats are reserved by sending a link |
| Seat holds and user payment | District's documented authenticated booking operations | Provider describes it, but our third-party app access is restricted; PVR lookup MCP does not establish hold/payment access |
| Posters/movie picker | TMDB metadata | Useful later; provider IDs must still be verified independently |
| Demand/occupancy dashboard | CinemaIP | Optional analytics; hourly data must not drive immediate availability claims |

## Recommended implementation order

1. Keep the running application and its booking-only Telegram contract unchanged.
2. Validate a **separate native PVR adapter in dry run**, initially for a few
   verified Chennai venues and fixed requested dates. Map PVR film prints,
   cinema IDs and start times explicitly; reject title/language/date conflicts.
3. Introduce a provider-specific seat-inventory record with observation time,
   category identity, total mapped seats and available counts. Missing or
   conflicting fields are unknown. Keep valid opening detection independent
   of optional numeric enrichment.
4. Share one cinema/date schedule fetch across up to five movie watches. Fetch
   seat maps only for fresh candidate openings/explicit seat watches, within a
   bounded provider-wide budget. Back off the provider on 403/429. Do not use
   the community proxy or shared hosted endpoint to evade an access denial.
5. Preserve all existing deduplication keys/receipts and silent startup semantics.
   Adding a native PVR offer must not replay an alert already sent for the same
   canonical cinema/show. Counts must be labelled **PVR-reported**, never copied
   into a District/BookMyShow inventory row or summed across platforms.
6. Validate across multiple cinemas, categories, dates and failure cases before
   live alert enrichment. Counts can change between collection and delivery;
   always expose the check time. Do not add restock/reopening alerts unless
   the user explicitly changes the current policy.
7. Pursue District product access only if desired later; permission, client
   registration, rates, commercial terms and authentication come before integration.

An MCP client alone requires no LLM token spending for scheduled calls. Using
paid AI chat features or hosted infrastructure is a separate cost decision.
Neither open-source code nor today's anonymous service access guarantees a
permanent zero-cost, unrestricted, complete inventory feed.
