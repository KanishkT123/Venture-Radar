# Venture Radar

A private investment-intelligence workspace for Cicada Health, configurable for any venture area. Two dashboard tabs, cited public-news research using OpenAI, saved investor notes, valuation comparables, and evidence-based introduction routes.

## Run on your computer

Requires Python 3.11 or newer. The local application uses only the Python standard library; Node and paid news subscriptions are not required.

```sh
git clone https://github.com/KanishkT123/Venture-Radar.git
cd Venture-Radar
cp .env.example .env
# Edit .env and set OPENAI_API_KEY. Never commit this file.
python3 -m radar serve
```

On Windows, copy `.env.example` to `.env` and run `py -m radar serve`. Open <http://localhost:8765>. Without a key you can explore sourced starter records, import/review investments, save notes, and build your network. With a key, click **Run research**. API usage is billed to the supplied OpenAI project.

The default focus is saliva biosensing, hormone monitoring, sleep/circadian health and wearable diagnostics. Change the venture, research focus, news query, watchlist and ranking weights in the UI. Start with `python3 -m radar serve --empty` for a new workspace without historical starter data.

Your database is `data/radar.sqlite3`. Keep this folder to preserve data between launches. Use `RADAR_DATA_DIR` to select another directory. Do not put the working SQLite database in a live file-sync folder. To back it up:

```sh
python3 -m radar backup ./backup.sqlite3
```

Restore with the server stopped: preserve the existing data folder, create a fresh data directory, copy the backup to `radar.sqlite3` there, then point `RADAR_DATA_DIR` to it. The dashboard also exports JSON (including private notes/network) and CSV. JSON export is an archive; the import UI accepts investment records, not complete workspace restores.

## What the two tabs do

**Investment dashboard:** company, round, amount, original currency, investors/grantors, announcement date, source evidence, review status, quarterly activity and reported valuations. Grants, debt and acquisitions are labeled. Undisclosed amounts and currencies remain unknown; a disclosed amount requires a sourced currency code. Non-USD amounts are not silently converted. Cumulative round totals are excluded from funding sums to reduce double counting.

**Investor dashboard:** firms ranked by largest tracked financing and recency, with thesis, partners, stage/geography, fit notes, watchlists and your own notes. This ranks participation in financing rounds; the total round is not an investor's individual check. Disclosed checks are stored separately.

The score is `100 × [w × min(1, ln(1 + biggest_round_USD) / ln(1 + 1 billion)) + (1 − w) × 2^(−days_since_latest / half_life)]`. Defaults are 55% size and a 180-day half-life. Only confirmed equity/grant records with non-cumulative amounts contribute; unknown amounts have zero size contribution. Profiles without investment evidence have zero activity. Activity is not a fit score or a probability of funding.

**Partner deep dives:** the current top five investors have a **Deep dive** button. A cited research workflow finds a relevant current partner (or grant programme lead), public professional contact, theses, interests, relevant writings, attributed investment outcomes and a personalized draft. Emails are never guessed; general firm inboxes are labeled and missing addresses remain unknown. Activity shows progress, then **Partner profile** opens the saved result. Edit, save or copy the message; nothing is sent. Refreshing research preserves saved draft edits. Profiles remain accessible if rankings change, and all trigger types share the daily run cap.

**Warm introductions:** add directed relationships from `You` to people/companies and onward to a firm. The app ranks paths of up to three hops, preferring confirmed relationships. Public portfolio connections are marked unverified. It cannot infer your private network from public news and never invents a mutual contact or sends outreach. Contact willingness still needs checking. The route-strength score is a heuristic, not a probability.

## Research and reusable OpenAI skills

The Python workflow loads these same instruction files used by Codex:

* `skills/discover-venture-investments/SKILL.md`
* `skills/research-investor-fit/SKILL.md`
* `skills/plan-warm-intros/SKILL.md`
* `skills/research-investor-partner/SKILL.md`

Copy a skill directory into your Codex skills directory to use it interactively, or leave the bundled skills in place for the app. The introduction UI uses a deterministic graph search; the warm-intro skill gives an analyst a reusable research/outreach-planning procedure.

Discovery combines public Google News RSS headlines with [Dealroom’s free funding signals](https://dealroom.co/for-agents/) as leads, then uses the OpenAI Responses API web-search tool to find cited evidence. A second, strict JSON-schema call extracts facts. Only cited source URLs may be stored. All AI-discovered investments enter **Needs review**; a human confirms or rejects them. A source citation does not prove every extracted claim is correct. No paywall bypass or comprehensive private-market database is included.

Every discovery run polls `https://dealroom.co/api/live-signals?limit=20` without a Dealroom key. This is a limited global sample, not the paid Dealroom API; it may contain old or irrelevant rounds and may omit valuations. The workflow checks relevance and announcement dates through web search. Feed timestamps and month-only labels are never treated as exact announcement dates. A feed outage does not stop the other sources or web research. Activity shows each feed’s lead count, upstream timestamp when available, and any availability warnings. Lead counts are not verified investment counts. This is scheduled polling, not a Dealroom-pushed webhook. See [Dealroom’s API overview](https://dealroom.co/products/api/) for the separate licensed product.

The included eight announcements and fifteen profiles are a selective historical starter set, not a comprehensive or current sector census. Sources are linked in each record. Partner roles and investment theses can become stale. Valuation fields distinguish pre-money, post-money and unspecified basis; missing values stay unknown. Compare business model, stage and date before using a valuation as a comparable.

The app sends research focus, watched firm names, news tips and public-source text to OpenAI. It does **not** send private investor notes, saved draft edits or the relationship graph. Calls use `store: false`. Keys remain server-side. `OPENAI_MODEL` is configurable; the default is `gpt-5.5`, and your account must support the selected model, web search and structured output.

## Automatic updates

### Local schedule

Enable automatic research in Workflow settings. The server checks the interval while your computer is awake and the process is running. For an OS scheduler with the server stopped, run `python3 -m radar refresh`. A data-directory process lock prevents two local worker owners. If the server is running, use its authenticated `POST /api/refresh` endpoint instead.

### Vercel

The included `vercel.json` deploys a Python ASGI API and the static UI. Create a Vercel project from this repository with framework **Other**, no build command, and output directory `static`.

1. Create a **private Vercel Blob store** and connect it to the project. It supplies `BLOB_READ_WRITE_TOKEN`.
2. Set server-side environment variables `OPENAI_API_KEY`, a random `RADAR_ACCESS_TOKEN`, `RADAR_WEBHOOK_SECRET`, and `CRON_SECRET`. Generate each application secret with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Deploy to production. Use the access token to unlock the app. If Vercel Authentication is enabled, sign into Vercel first.

The deployed cron checks once daily around 08:00 UTC, including on the Hobby plan (execution within the hour may vary). A newly created hosted workspace enables automatic research with a 24-hour minimum gap. No research runs without an OpenAI key. Changing the UI's interval does not change Vercel's cron frequency; update `vercel.json` for a different plan-supported schedule. Webhooks and manual requests can also enqueue immediate work.

Hosted data is a small SQLite snapshot saved to private Blob, with uncached origin reads and conditional ETag writes. Conflicting mutations are retried against the newest snapshot; expensive OpenAI calls are not repeated for storage conflicts. Production and preview use separate object paths. This preserves data across cold starts and redeployments. This implementation is intentionally bounded to **one workspace and 8 MB**. It transfers the snapshot per request/write; migrate to PostgreSQL for larger datasets, many users, or high write volume. Vercel compute/storage charges may apply. Export backups regularly.

Jobs are saved before execution. The ASGI request stays alive for background processing after the 202 response. Research has a 220-second client time budget inside a 300-second function. Interrupted jobs are marked failed after ten minutes on the next worker/cron check; retries are manual to avoid unnoticed repeated API charges. Queued work can resume on a dashboard poll or the next cron. This is not a multi-stage durable workflow engine; unusually slow research may time out.

### News webhook

`POST /api/webhooks/news` accepts `{"title":"...","url":"https://...","summary":"..."}`. It treats the message as a research lead, never as a verified investment. It requires these headers:

* `Content-Type: application/json`
* `X-Radar-Timestamp`: Unix seconds, within five minutes
* `X-Radar-Delivery`: unique stable delivery ID, reused on retries
* `X-Radar-Signature`: `sha256=` plus HMAC-SHA256 of `timestamp + "." + delivery_id + "." + exact_request_body`, keyed with `RADAR_WEBHOOK_SECRET`

See `scripts/send_news_tip.py` for a working sender. Store the secret in the sender's environment. A Zapier, Make, n8n, news-monitoring service or your own website can call this when its feed detects a major investment. Most news sites do not emit investment-specific webhooks, so scheduled discovery is the default detection mechanism. The app flags major findings in Activity; it does not send email or Slack notifications.

The endpoint must be reachable from the sender. On a local computer that requires an approved HTTPS tunnel and the computer to remain awake. For a Vercel deployment protected by Vercel Authentication, an external sender also needs an owner-approved Vercel automation access mechanism. The deployed application does not disable platform protection. Never publish an access token or a platform bypass secret.

All trigger types share a rolling 24-hour research-run cap (default eight). Up to six search-tool calls per discovery or firm run, and eight per partner deep dive. This is a run cap, not a dollar budget. Set OpenAI project spending limits too. Delivery IDs prevent duplicate webhook jobs; revised investment facts enter review instead of silently overwriting confirmed records.

## GitHub and development

The source contains no API keys, live database, private network or deployment access file. Create an empty repository, then:

```sh
git init
git add .
git commit -m "Add Venture Radar dashboard and research workflows"
git branch -M main
git remote add origin <your-repository-url>
git push -u origin main
```

Connect the repository to Vercel for deployment on pushes. Python-only checks:

```sh
python3 -m unittest discover -s tests -v
```

Thirty-four tests cover validation, auth, signed webhooks, deduplication/revisions, persistence, ranking, introduction paths, research provenance, combined-source discovery, feed failures/limits/redirects and cloud write/claim concurrency. Live OpenAI calls require your key and are not part of the offline suite. See `docs/architecture.md` for deployment details. MIT licensed.
