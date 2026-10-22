# Automatic discovery of new MBA internship postings — design

Date: 2026-10-22 · Status: draft for owner approval

## Intent

The list should notice when companies post Summer 2027 MBA internships (and when postings come down) and update itself, so it stays current through recruiting season without the owner doing the legwork. It is used by MBA students at every school.

The owner's constraints, in his words: **free for him to run, as simple as possible, not on his machine.** He likes how the README looks today and doesn't want that changed.

## Goals

- New in-scope postings at tracked companies show up as a ready-to-merge pull request within about a day.
- Postings that come down get marked closed by the same route.
- Every published field follows CONTRIBUTING.md exactly: pay, sponsorship and deadline only as the posting states them, the company's own posting as the link.
- No cost beyond the owner's existing Claude subscription, and nothing running on his machine.

## Non-goals

- Changing the README format, categories or scope rules.
- Automating Wharton CareerPath. It needs the owner's PennKey login, which must never be stored anywhere. It stays an occasional manual cross-check in an interactive Claude Code session.
- Covering sites that can't be read without a browser or login: Meta (GraphQL with a token) and Uber (403/406). Their rows stay `expected` and are handled by hand or by community issues.
- Auto-merging. A human merges every change.

## Anti-goals (this would be a failure even if it technically worked)

- A wrong pay, sponsorship or deadline reaches the public list because the owner rubber-stamped a PR.
- Any path where text from a job posting or a public issue can get a commit onto `main` without the owner's merge.
- The owner has to babysit it: daily PRs full of noise, the same rejected candidate re-proposed every day, or silent breakage he only notices weeks later.
- It quietly stops working mid-season.

## How it works (decided)

Three pieces, with as few moving parts as possible.

### 1. Watchlist + deterministic scanner (in the repo)

- `watchlist.json`: one entry per company with its ATS type, the identifiers needed to query it, and the search terms to use. The research on 2026-10-22 mapped 61 of 65 tracked companies. It covered Greenhouse, Ashby, Lever, Workday CXS, Oracle HCM, Eightfold, iCIMS/Jibe, Radancy, Avature, amazon.jobs, Google, Apple, TikTok, IBM and Atlassian, and 89 of 91 open rows were retrievable. Recipes are recorded in "Reference: ATS recipes" below.
- `scripts/discover.py`: no AI, standard library only (matching the existing scripts). For each watchlist company it runs broad queries ("MBA" and "intern", per company), pages through all results and filters in code:
  - Keep postings whose title or body looks like a Summer 2027 internship. This means (intern | summer associate | graduate internship | summer 2027) and (MBA | graduate/masters).
  - Use word-boundary matching so "mba" doesn't match "Mumbai".
  - Drop anything already in `listings.json` by job ID or URL, and anything in `ignored.json`.
  - Flag a likely repost when a listed row's job ID has vanished and a near-identical title appears at the same company.
  - It writes `candidates.json` with company, title, URL, job ID and the fetched posting text.
  - A feed that errors or suddenly returns zero jobs is reported as a feed failure, never as "nothing new".
- `ignored.json`: postings that were reviewed and rejected (out of scope, not MBA-level, full-time). Each entry has its job ID or URL and a one-line reason, so a rejected posting is never proposed again.

### 2. One Claude scheduled task (owner's subscription)

A Claude Code cloud scheduled task (claude.ai/code/routines) runs daily. Its saved prompt is short: "Follow `AUTOMATION.md` in the repo". The real instructions live in the repo, so they are version-controlled and reviewable. Each run:

1. Runs `python3 scripts/discover.py`. If it finds no candidates, no repost and no feed failure, it stops. That costs almost nothing.
2. For each candidate, decides scope against CONTRIBUTING.md. In-scope candidates become a `listings.json` row, filled only from the fetched posting. Rejected ones get an `ignored.json` entry with a reason.
3. Applies closures. It runs `scripts/check_links.py --json`, then marks a row closed only when its posting is confirmed gone: dead, or closed by the ATS check. Past-deadline rows are marked closed only if the posting is also gone.
4. Regenerates the README, runs `update_readme.py --check` and the new validator, then pushes a `claude/` branch and opens **one PR per run**. The PR body has a table for every new or changed row with field, value and the exact quoted line from the posting, plus a list of rejected candidates with reasons, plus any feed failures.
5. If anything needs the owner's attention, says so at the top of the PR body. That includes a broken feed, a candidate it couldn't decide on, or a new company that isn't on the watchlist yet. If there's no PR to open, it opens a GitHub issue instead.

Facts about scheduled tasks that this design relies on (verified in the docs on 2026-10-22):
- They use subscription usage with no separate bill.
- They run at most hourly.
- They clone the repo and push `claude/`-prefixed branches as the owner's GitHub identity.
- The Default environment's "Trusted" network blocks hosts outside a default allowlist, so the task needs a Custom environment.
- If GitHub is disconnected for 72 hours, the task turns itself off.
- A green run status only means no infrastructure error.

### 3. GitHub checks on every PR (existing Actions, free)

- The existing `check-readme.yml` (`update_readme.py --check`) runs on the PR. PRs opened under the owner's identity trigger workflows normally, unlike `GITHUB_TOKEN` PRs.
- New `scripts/validate_listings.py`, run in the same workflow. For every row that is added or changed in the PR, it re-fetches the posting and checks:
  - Any non-null `pay` has its dollar figures present in the posting text.
  - A `deadline` has its date present in the text, in any common format.
  - `sponsorship` yes or no appears alongside sponsorship or authorization wording.
  - The `url` host is one of that company's known hosts from the watchlist.
  - `notes` contains no URLs or markdown links and stays under a length cap.
  - If the posting can't be fetched, the check fails with "verify by hand" rather than passing.
- The weekly link check keeps running as it does today.

### Safety settings (one-time setup by the owner)

- A ruleset on `main` that requires a pull request and **applies to admins with no bypass**. The docs say a run's push is checked against the owner's own access, so a rule he can bypass wouldn't stop it.
- The task's cloud environment uses **Custom** network access, allowing only the ATS and career hosts in the watchlist plus GitHub. Connectors are removed from the task.
- The task is triggered only by its schedule. There are no issue or PR triggers, so public text can't start a run.

## Decisions made after builder review (Claude's calls; owner may override)

1. **One rolling PR, never duplicates.** If a `claude/` listing-update PR is already open, the run checks out that branch and adds to it rather than opening a second PR. It also dedupes candidates against that branch, not just `main`. It rewrites the PR body so the evidence table always covers the whole PR. If the owner is away for a week, he comes back to one PR, not seven.
2. **The validator is a required check, scoped to what changed.**
   - It checks only fields that were added or changed in the PR.
   - A change that only sets `status` to `closed` skips the fetch, since the posting is gone by definition.
   - If a posting can't be fetched (403 or bot wall), the check passes only when the changed row has no claims to verify: `pay` null, no `deadline`, `sponsorship` unknown. Otherwise it fails with "verify by hand".
   - This keeps the check meaningful without blocking closures or hand-maintained Meta/Uber rows.
3. **`expected` placeholders get converted, not duplicated.** When a real posting appears at a company with an `expected` row for the same kind of role, that row becomes `open` with the real title, URL and fields. Today's Duolingo, Airbnb and Google rows were handled this way. Any further postings at that company become new rows. Placeholders are never deleted; one is converted or left as is.
4. **Reposts are updated in place.** When a listed req disappears and a near-identical title appears at the same company, the existing row's URL and fields are updated, and the note records the repost. This follows the Capital One precedent from 2026-10-22. It is never close-and-add.
5. **Heartbeat.** Every run, including a quiet one, posts a one-line comment on a pinned "Automation status" issue: date, feeds OK or failed, candidates found. The existing weekly link-check workflow also checks that issue. If the last comment is more than 3 days old, it says so in the weekly issue and notifies the owner. That issue already exists and already notifies him, so no new component is needed to catch a silently disabled task.
6. **What counts as a feed failure.** A broad query that errors, or returns zero results before any filtering, is a failure. Zero *in-scope* matches is normal, especially Feb–Jul.
7. **"New company not on the watchlist"** means a company that appears in `listings.json` (added by hand or through a community issue) with no watchlist entry. The run proposes the entry in its PR. Finding companies the list has never tracked is out of scope for v1. That still comes from people: the owner's manual sessions, CareerPath cross-checks and community issues.

## Cadence

Daily, early morning US Eastern, a few minutes past the hour. Off-season (Feb–Jul) runs that find nothing cost almost nothing, so the schedule doesn't need to change by season. The owner may switch to weekly in spring if he likes.

## Rollout

1. Build the scanner and validator, and replay them against today's 124 rows. Report how many listed open rows the scanner would have found (target: matching the 89 of 91 measured in research), and how many false candidates per run it raises before rejections are recorded.
2. Seed `ignored.json` from that first run, so day one doesn't produce a PR full of noise.
3. Do one manual "Run now" of the task and review the PR it opens together.
4. Turn on the daily schedule.

## Left to the builder

- Exact filter keywords and thresholds, tuned during the replay in Rollout step 1.
- Code structure: one ATS adapter per function in `discover.py`, as in `check_links.py`.
- Wording of `AUTOMATION.md`, which must restate the CONTRIBUTING.md rules and the PR-body format above.
- Rate-limit handling. Eightfold needs about 4s spacing and backoff; elsewhere, polite delays.
- How repost detection matches titles (normalised title stem plus company is the suggested key).

## Reference: ATS recipes (from 2026-10-22 research)

| ATS | Endpoint |
| --- | --- |
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/<token>/jobs` |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/<org>` |
| Lever | `GET api.lever.co/v0/postings/<co>?mode=json` |
| Workday | `POST https://<host>/wday/cxs/<tenant>/<site>/jobs`, body `{"appliedFacets":{},"limit":20,"offset":N,"searchText":"..."}` |
| Oracle HCM | `GET <host>/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber=<site>,limit=25,offset=N,keyword=...` |
| Eightfold | `GET <host>/api/pcsx/search?domain=<domain>&query=...&start=N` |
| amazon.jobs | `GET www.amazon.jobs/en/search.json?base_query=MBA&result_limit=100&offset=N&sort=relevant` |
| Google | results page HTML; regex the embedded job IDs |
| Apple | search page HTML; regex `/en-us/details/(\d+)` |
| TikTok | `POST api.lifeattiktok.com/api/v1/public/supplier/search/job/posts` with `website-path: tiktok` |
| IBM | `POST www-api.ibm.com/search/api/v2` |
| iCIMS/Jibe | `GET careers.<co>.com/api/jobs?keywords=...` |
| Radancy | `GET <host>/search-jobs/results?...&Keywords=...` with `X-Requested-With: XMLHttpRequest` |
| Avature (EA) | search page HTML |
| Atlassian | `GET www.atlassian.com/endpoint/careers/listings` |

Workday notes:
- `limit` is capped at 20.
- The pod and site must be exact: a wrong site returns 422, a wrong pod 401.
- Phenom and Radancy front ends usually sit on Workday, Ashby or Greenhouse; query the underlying ATS.

Search notes:
- Amazon results must be sorted by relevance.
- Some postings never appear in a site's search index (seen with Amazon India and one TikTok req). Those rows stay covered by the link checker only.

Uncovered: Meta, Uber, Walmart and Bank of America campus.
