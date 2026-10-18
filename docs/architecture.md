# Architecture and operations

UI → authenticated Python API → Store interface. Local uses SQLite directly. Vercel uses conditional private Blob snapshots, materialized in temporary directories only for a request. Temporary filesystem contents are never the source of truth.

Manual trigger / scheduler / signed webhook → persisted job → atomic claim → public RSS + Dealroom free funding leads → cited OpenAI web search → schema extraction → validation and provenance check → saved review records + profiles → browser refresh. Confirmed records feed headline metrics and investor ranking.

Investment identity normalizes company/round with a 14-day announcement window. Exact financial duplicates merge sources. Changed amounts, investors or valuations create review revisions; confirming one revision rejects related versions. This heuristic cannot resolve all legal entity aliases or distinguish every extension: review related announcements and use explicit corrections where needed.

`radar/server.py` owns the HTTP contract. `api/index.py` adapts it to ASGI, supplies the Vercel cron endpoint and runs bounded background processing. `radar/cloud.py` implements the Blob transport using the REST headers used by official @vercel/blob 2.8.2: `x-if-match`, `x-allow-overwrite` and private origin `?cache=0`. A live storage check verified stale ETag rejection. No authentication token is included in browser code or URLs.

## Boundaries

* A single shared dashboard access token, not multi-user roles or per-user audit attribution.
* Local server binds to loopback by default; other interfaces require an access token. Production must use HTTPS.
* UI escapes untrusted text and restricts links to HTTP(S). API blocks cross-origin browser requests and caps request bodies. CSV exports neutralize formula prefixes.
* Webhook HMAC includes delivery ID and timestamp, is compared in constant time, and delivery IDs are persisted. The server does not fetch arbitrary webhook URLs directly.
* Research sources and news content are untrusted data. Skills explicitly prohibit obeying source instructions and inventing missing information.
* Source citations constrain provenance, not factual entailment. Investments need human review. Investor profile research is labeled for verification.
* Hosted writes may fail under prolonged contention; callers receive an error instead of overwriting a newer snapshot. Retry the action. Blob is not a general relational database service; the adapter is appropriate only for this small workspace.
* Some cloud operations are multiple mutations (for example, a multi-record manual import). A service failure can save an earlier subset. Reimporting exact records is deduplicated. Research batches upload together in one conditional snapshot.
* Timeout after an OpenAI submission can still incur usage. Failed jobs are visible. No automatic retry after uncertain cloud timeouts.
* Seed data is public, curated history. Your notes and contact graph are private workspace data; include them only in backups you control.

## Hosted backup and recovery

Export JSON from the authenticated dashboard for a readable archive. For an exact restore, use the Vercel Blob dashboard to download `venture-radar/production.sqlite3` (private). Retain dated copies securely. Stop incoming writes before owner-controlled recovery, preserve the existing blob, and replace only the intended workspace snapshot. Restoring an old snapshot also restores old jobs and webhook delivery history; review these before resuming automation. Never publish a database backup in a GitHub repository.

## Verification performed

Offline unit and HTTP tests cover the API-to-database flow, simulated two-stage OpenAI extraction, and cloud conflict replay. A real private Vercel Blob object was written, reread, and checked for stale-write rejection. The production API returned eight investment records and fifteen investors; a setting change survived a new request and was restored. Unauthorized API access returned 401; research without a key returned 400. Paid OpenAI discovery has not been exercised without a supplied key. Browser visual QA was blocked by the managed preview environment's mount error; rendering has not been visually verified.

## Public funding source integration

`radar/feeds.py` fetches only Dealroom’s public `live-signals` endpoint, with a 12-second timeout, 1 MB response cap, same-host HTTPS redirects and at most 20 deduplicated funding leads. It preserves ambiguous date and amount labels for subsequent verification. No Dealroom credentials, institutional PitchBook access or paid datasets are used. Each feed fails independently; the run records feed availability, counts and Dealroom’s reported fetch timestamp. Those diagnostics persist with the research job and appear in Activity. The two-stage cited research and review requirements are unchanged.

Integration verification on 2026-10-18: all 26 offline tests passed. The live Dealroom adapter returned 10 leads. Google News RSS returned an HTTP error in the test environment; the combined workflow continued, passed Dealroom leads to the simulated OpenAI client, and persisted the source diagnostics. Live paid OpenAI extraction remains untested until a key is supplied. Activity markup was checked for counts and availability messages; no visual layout changes were made.

## Partner profiles and currency validation

Partner jobs share the existing authenticated queue, rolling daily cap and server-side OpenAI client. The API limits new deep dives to the current top five rankings and deduplicates active requests for the same firm. Cited web research precedes strict structured extraction; nested evidence URLs must be among cited report sources. A new additive `partner_profiles` SQLite table stores research and separate user-edited draft fields. Cloud writes use the existing conditional Blob update mechanism. Refreshes preserve edits and never include private notes, drafts or network data in the model input. There is no sending endpoint.

Discovery previously allowed arbitrary currency text in the extraction schema while validation accepted only a three-letter code. The schema now constrains known currencies, permits null for undisclosed amounts and never defaults an unknown amount's currency to USD. Quantified events without a sourced code are rejected with a company-specific error. Legacy unknown labels on undisclosed amounts normalize to null.

The 34-test offline suite covers the currency regression, partner provenance, missing and general contacts, authenticated top-five eligibility, duplicate requests, shared limits, saved draft persistence across SQLite and cloud refreshes, export and exclusion of private context. Earlier deployment notes above describe the prior release.
