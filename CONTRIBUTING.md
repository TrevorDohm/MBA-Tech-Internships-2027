# Contributing

This list only stays useful if people keep it current. Every new posting, closed role, or broken link you report helps the next person.

## The easy way: open an issue

- **Found a new role?** Use the [➕ New internship](../../issues/new?template=new_role.yml) form.
- **Role closed, link broken, or info wrong?** Use the [✏️ Update or close a role](../../issues/new?template=update_role.yml) form.

A maintainer will add it to `listings.json` and regenerate the README.

## The direct way: open a pull request

1. Edit [`listings.json`](listings.json). Each role is one object:

   ```json
   {
     "company": "Google",
     "role": "MBA Product Manager Intern, Summer 2027",
     "category": "PM",
     "locations": ["Mountain View, CA", "New York, NY"],
     "url": "https://careers.google.com/jobs/results/...",
     "date_posted": "2026-10-01",
     "deadline": "2026-10-31",
     "status": "open",
     "pay": "$55–$65/hr",
     "sponsorship": "unknown",
     "notes": "Must be enrolled in a full-time MBA graduating in 2028"
   }
   ```

   | Field | Allowed values |
   | --- | --- |
   | `category` | `PM`, `Strategy & BizOps`, `PMM & GTM`, `Finance & Corp Dev` |
   | `status` | `open`, `closed`, `expected` (company usually hires MBA interns, but the 2027 posting isn't up yet) |
   | `sponsorship` | `yes`, `no`, `unknown` (go by what the posting says) |
   | `date_posted` | `YYYY-MM-DD`, or `null` if unknown |
   | `deadline` | Optional. `YYYY-MM-DD`, only when the posting states a closing date. Shows as **Apply by …** at the start of the notes, so don't repeat it there. |
   | `pay` | Only what the posting publishes (pay-transparency ranges count). **Never estimate.** Use `null` if not listed. |

2. Regenerate the README:

   ```bash
   python3 scripts/update_readme.py
   ```

3. Commit both `listings.json` and `README.md`. CI runs `python3 scripts/update_readme.py --check` and fails if they're out of sync or a field is invalid.

## Checking links

```bash
python3 scripts/check_links.py             # markdown report
python3 scripts/check_links.py --json      # same, as JSON
python3 scripts/check_links.py --no-fetch  # deadlines only, no network
python3 scripts/check_links.py --strict    # exit 1 if anything needs action
```

It fetches every open and expected row and reports:

- **Dead**: the link returns 404/410.
- **Closed**: the posting is gone. Greenhouse, Workday and amazon.jobs links are checked against the ATS itself; other pages are scanned for "no longer available"-style text.
- **Past deadline**: an open row whose `deadline` has passed.
- **Unknown**: 401/403/429/5xx or a timeout. That's usually bot protection, so it's never treated as dead; check by hand if it keeps showing up.

A [weekly workflow](.github/workflows/check-links.yml) runs it every Monday and keeps a single **Weekly link check** issue open while anything is dead, closed, or past its deadline. Expect false alarms now and then: confirm on the posting before closing a row.

## What counts

- ✅ Summer 2027 internships aimed at **full-time MBA students** (or roles that explicitly accept MBAs) at tech companies, or in the tech/product org of a non-tech company
- ✅ PM, strategy/BizOps, product marketing/GTM, and finance/corp dev roles
- ❌ Undergrad, PhD-only, or SWE/research internships (there are [great lists](https://github.com/SimplifyJobs/Summer2026-Internships) for those)
- ❌ Links to reposts or aggregators. Please link the company's own posting.

## Closing roles

When a posting comes down, set `"status": "closed"` instead of deleting it. Closed roles show up struck through with a 🔒, which helps next year's class see who hired and when.

## Automated updates

A daily scheduled Claude task scans the companies in [`watchlist.json`](watchlist.json) and keeps one open pull request (branch `claude/listing-updates`) with new postings, reposts and closures. Its instructions, and the one-time owner setup, are in [AUTOMATION.md](AUTOMATION.md). A human reviews and merges every change; each pay, deadline or sponsorship value in the PR carries a quote from the posting.

[`ignored.json`](ignored.json) lists postings that were reviewed and rejected (out of scope, not MBA-level, full-time), so they are not proposed again. If a rejected posting is actually in scope, delete its entry and it will be picked up on the next run, or add it by hand.

## License

This project is released under [CC0 1.0](LICENSE). By contributing, you agree to release your contribution under the same terms.
