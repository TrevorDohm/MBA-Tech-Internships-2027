#!/usr/bin/env python3
"""Adapters that list and read postings from company job sites (ATS feeds).

    list_jobs(entry, http=fetch)         -> [Job]   every query, all pages, deduped by id
    posting_text(entry, job, http=fetch) -> str     plain text of one posting

A Job is {"id", "title", "url", "location"}; `url` is the public posting URL.
`entry` is a watchlist.json entry. `http(url, method="GET", body=None,
headers=None)` returns (status, text) and is injected by tests.

    python3 scripts/ats.py --record COMPANY   # rewrite tests/fixtures for COMPANY's ATS
"""

import html
import json
import re
import sys
import time
from http.client import HTTPException, IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from check_links import TIMEOUT, UA
from watchlist import load_watchlist

MAX_BODY = 5_000_000  # Google's detail pages exceed check_links' 1 MB cap
WORKDAY_LIMIT = 20  # Workday rejects larger pages
ORACLE_LIMIT = 25
EIGHTFOLD_DELAY = 4  # seconds between Eightfold requests; it 429s on quick bursts
MAX_PAGES = 25
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# Oracle tenants whose public careers site is not the API host: host -> (host, path).
ORACLE_PUBLIC = {
    "egug.fa.us2.oraclecloud.com": ("careers.americanexpress.com", "/en/sites/{site}/job/{id}"),
}
ORACLE_DEFAULT_PATH = "/hcmUI/CandidateExperience/en/sites/{site}/job/{id}"


class FeedError(Exception):
    """A feed that errored, returned something unreadable, or had no results at all."""


def fetch(url, method="GET", body=None, headers=None):
    """Return (status, text). status is 0 on network errors."""
    hdrs = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"}
    hdrs.update(headers or {})
    data = body.encode("utf-8") if isinstance(body, str) else body
    try:
        with urlopen(Request(url, data=data, headers=hdrs, method=method), timeout=TIMEOUT) as resp:
            try:
                raw = resp.read(MAX_BODY)
            except IncompleteRead as e:
                raw = e.partial
            return resp.status, raw.decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, ""
    except (URLError, HTTPException, OSError) as e:
        return 0, str(getattr(e, "reason", e))


# --- helpers -----------------------------------------------------------------

_BLOCK = re.compile(r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|td|th|table|section|article|header|footer|main)\b[^>]*>", re.I)


def text_of(markup):
    """Strip tags and entities from HTML and collapse whitespace."""
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup or "")
    s = _BLOCK.sub(" ", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _job(id_, title, url, location=""):
    return {"id": str(id_), "title": text_of(title), "url": url, "location": text_of(location or "")}


def _get(http, url, headers=None):
    status, text = http(url, method="GET", body=None, headers=headers)
    if status != 200:
        raise FeedError(f"HTTP {status} from {url}")
    return text


def _loads(text, url):
    try:
        return json.loads(text)
    except ValueError:
        raise FeedError(f"unreadable JSON from {url}")


def _get_json(http, url, headers=None):
    return _loads(_get(http, url, headers), url)


def _post_json(http, url, payload, headers=None):
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    hdrs.update(headers or {})
    status, text = http(url, method="POST", body=json.dumps(payload), headers=hdrs)
    if status != 200:
        raise FeedError(f"HTTP {status} from {url}")
    return _loads(text, url)


def _parts(*pieces):
    return " ".join(p for p in (text_of(x) for x in pieces) if p)


def _slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _ld_description(page):
    for m in re.finditer(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting" and d.get("description"):
            return d["description"]
    return None


def _main(page):
    m = re.search(r"(?is)<main\b.*?</main>", page)
    return m.group(0) if m else page


def _match_bracket(s, i):
    """Index just past the ']' closing the '[' at s[i], aware of JSON strings; -1 if none."""
    depth, in_str, esc = 0, False, False
    for j in range(i, len(s)):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return j + 1
    return -1


# --- Greenhouse: one call returns the whole board -----------------------------

def _list_greenhouse(entry, http):
    d = _get_json(http, f"https://boards-api.greenhouse.io/v1/boards/{entry['board']}/jobs")
    out = []
    for j in d["jobs"]:
        url = j["absolute_url"]
        # Drop the query when the path still names the job; custom career sites
        # (Waymo, Epic) only identify it by ?gh_jid=, so keep that.
        if str(j["id"]) in urlsplit(url).path:
            url = url.split("?")[0]
        out.append(_job(j["id"], j["title"], url, (j.get("location") or {}).get("name")))
    return out


def _text_greenhouse(entry, job, http):
    d = _get_json(http, f"https://boards-api.greenhouse.io/v1/boards/{entry['board']}/jobs/{job['id']}")
    return text_of(html.unescape(d.get("content", "")))  # content arrives entity-escaped


# --- Ashby: whole board in one call, descriptions included ---------------------

def _ashby_board(entry, http):
    return _get_json(http, f"https://api.ashbyhq.com/posting-api/job-board/{entry['org']}")["jobs"]


def _list_ashby(entry, http):
    return [_job(j["jobUrl"].rstrip("/").rsplit("/", 1)[-1], j["title"], j["jobUrl"], j.get("location"))
            for j in _ashby_board(entry, http)]


def _text_ashby(entry, job, http):
    for j in _ashby_board(entry, http):
        if j["jobUrl"].rstrip("/").endswith("/" + job["id"]):
            return text_of(j.get("descriptionPlain") or j.get("descriptionHtml", ""))
    raise FeedError(f"Ashby posting {job['id']} not on the board")


# --- Lever ---------------------------------------------------------------------

def _list_lever(entry, http):
    d = _get_json(http, f"https://api.lever.co/v0/postings/{entry['org']}?mode=json")
    return [_job(j["id"], j["text"], j["hostedUrl"], (j.get("categories") or {}).get("location"))
            for j in d]


def _text_lever(entry, job, http):
    j = _get_json(http, f"https://api.lever.co/v0/postings/{entry['org']}/{job['id']}")
    lists = [f"{x.get('text', '')} {x.get('content', '')}" for x in j.get("lists") or []]
    return _parts(j.get("description") or j.get("descriptionPlain"), *lists, j.get("additional"))


# --- Workday: POST search, 20 per page, job id = externalPath suffix ----------------

def _wd_base(entry):
    return f"https://{entry['host']}/wday/cxs/{entry['tenant']}/{entry['site']}"


def _list_workday(entry, http):
    out = []
    for q in entry["queries"]:
        seen, offset, total = set(), 0, 0
        while True:
            d = _post_json(http, _wd_base(entry) + "/jobs",
                           {"appliedFacets": {}, "limit": WORKDAY_LIMIT, "offset": offset, "searchText": q})
            total = max(total, d.get("total") or 0)  # only the first page reports it
            fresh = [p for p in d.get("jobPostings") or []
                     if p.get("externalPath") and p["externalPath"] not in seen]
            if not fresh:  # empty page, or the site ignored offset and repeated itself
                break
            for p in fresh:
                seen.add(p["externalPath"])
                path = p["externalPath"]
                id_ = path.rsplit("_", 1)[-1] if "_" in path else path.rsplit("/", 1)[-1]
                out.append(_job(id_, p["title"], f"https://{entry['host']}/{entry['site']}{path}",
                                p.get("locationsText")))
            offset += WORKDAY_LIMIT
            if offset >= total:
                break
    return out


def _text_workday(entry, job, http):
    path = urlsplit(job["url"]).path
    i = path.find("/job/")  # tolerate /en-US/<site>/job/... in stored URLs
    if i < 0:
        raise FeedError(f"not a Workday posting URL: {job['url']}")
    info = _get_json(http, _wd_base(entry) + path[i:])["jobPostingInfo"]
    text = text_of(info.get("jobDescription", ""))
    if info.get("endDate"):
        text += f" Posting end date: {info['endDate']}"
    return text


# --- Oracle HCM -------------------------------------------------------------------

def _oracle_public(entry, id_):
    host, path = ORACLE_PUBLIC.get(entry["host"], (entry["host"], ORACLE_DEFAULT_PATH))
    return f"https://{host}{path.format(site=entry['site_number'], id=id_)}"


def _list_oracle(entry, http):
    out = []
    for q in entry["queries"]:
        offset = 0
        while True:
            finder = (f"findReqs;siteNumber={entry['site_number']},limit={ORACLE_LIMIT},offset={offset},"
                      f"keyword=%22{quote(q)}%22,sortBy=POSTING_DATES_DESC")
            d = _get_json(http, f"https://{entry['host']}/hcmRestApi/resources/latest/"
                                f"recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder={finder}")
            head = (d.get("items") or [{}])[0]
            reqs = head.get("requisitionList") or []
            out += [_job(r["Id"], r["Title"], _oracle_public(entry, r["Id"]), r.get("PrimaryLocation"))
                    for r in reqs]
            offset += ORACLE_LIMIT
            if not reqs or offset >= (head.get("TotalJobsCount") or 0):
                break
    return out


def _text_oracle(entry, job, http):
    d = _get_json(http, f"https://{entry['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                        f"?expand=all&onlyData=true&finder=ById;Id=%22{job['id']}%22,siteNumber={entry['site_number']}")
    if not d.get("items"):
        raise FeedError(f"Oracle posting {job['id']} not found")
    r = d["items"][0]
    flex = [f"{f.get('Prompt', '')}: {f.get('Value', '')}" for f in r.get("requisitionFlexFields") or []]
    text = _parts(r.get("ExternalDescriptionStr"), r.get("ExternalResponsibilitiesStr"),
                  r.get("ExternalQualificationsStr"), *flex, r.get("CorporateDescriptionStr"))
    if r.get("ExternalPostedEndDate"):
        text += f" Posting end date: {r['ExternalPostedEndDate']}"
    return text


# --- Eightfold: needs >= 4 s between requests ---------------------------------------

def _ef_json(http, url, used):
    """GET with spacing, and backoff on 429. `used` is truthy once a request has been made."""
    for attempt in range(4):
        if used or attempt:
            time.sleep(EIGHTFOLD_DELAY * (1 + 2 * attempt))
        used = True
        status, text = http(url, method="GET", body=None, headers=None)
        if status != 429:
            break
    if status != 200:
        raise FeedError(f"HTTP {status} from {url}")
    return _loads(text, url)["data"]


def _list_eightfold(entry, http):
    out, used = [], False
    for q in entry["queries"]:
        start = 0
        while True:
            qs = urlencode({"domain": entry["domain"], "query": q, "start": start, "sort_by": "timestamp"})
            d = _ef_json(http, f"https://{entry['host']}/api/pcsx/search?{qs}", used)
            used = True
            ps = d.get("positions") or []
            for p in ps:
                loc = p.get("standardizedLocations") or p.get("locations") or []
                out.append(_job(p["id"], p["name"], urljoin(f"https://{entry['host']}/", p.get("positionUrl")
                                                            or f"/careers/job/{p['id']}"), "; ".join(loc)))
            start += len(ps)
            if not ps or start >= (d.get("count") or 0):
                break
    return out


def _text_eightfold(entry, job, http):
    qs = urlencode({"position_id": job["id"], "domain": entry["domain"], "hl": "en"})
    d = _ef_json(http, f"https://{entry['host']}/api/pcsx/position_details?{qs}", False)
    return text_of(d.get("jobDescription", ""))


# --- Jibe (iCIMS front end) ------------------------------------------------------------

def _jibe_page(entry, http, keywords, page):
    qs = urlencode({"keywords": keywords, "page": page, "limit": 100})
    return _get_json(http, f"https://{entry['host']}/api/jobs?{qs}")


def _list_jibe(entry, http):
    out = []
    for q in entry["queries"]:
        seen = set()
        for page in range(1, MAX_PAGES + 1):
            d = _jibe_page(entry, http, q, page)
            fresh = [j["data"] for j in d.get("jobs") or [] if j["data"]["req_id"] not in seen]
            if not fresh:
                break
            for j in fresh:
                seen.add(j["req_id"])
                out.append(_job(j["req_id"], j["title"], f"https://{entry['host']}/careers-home/jobs/{j['req_id']}",
                                j.get("full_location") or j.get("location_name")))
            if len(seen) >= (d.get("totalCount") or 0):
                break
    return out


def _text_jibe(entry, job, http):
    for j in _jibe_page(entry, http, job["id"], 1).get("jobs") or []:
        if str(j["data"]["req_id"]) == job["id"]:
            d = j["data"]
            return _parts(d.get("description"), d.get("responsibilities"), d.get("qualifications"))
    raise FeedError(f"Jibe posting {job['id']} not found")


# --- Radancy / TalentBrew: JSON wrapping an HTML fragment -------------------------------

_RADANCY_ITEM = re.compile(r'<a\s+href="(/job/[^"]*?/(\d+))"[^>]*>(.*?)</a>', re.S)


def _list_radancy(entry, http):
    out = []
    for q in entry["queries"]:
        seen = set()
        for page in range(1, MAX_PAGES + 1):
            qs = urlencode({"CurrentPage": page, "RecordsPerPage": 200, "Keywords": q, "SearchType": 5,
                            "SearchResultsModuleName": "Search Results", "SearchFiltersModuleName": "Search Filters",
                            "ActiveFacetID": 0, "SortCriteria": 0, "SortDirection": 0})
            frag = _get_json(http, f"https://{entry['host']}/search-jobs/results?{qs}",
                             {"X-Requested-With": "XMLHttpRequest"})["results"]
            fresh = [m for m in _RADANCY_ITEM.finditer(frag) if m.group(2) not in seen]
            for m in fresh:
                seen.add(m.group(2))
                inner = m.group(3)
                loc = re.search(r'class="job-location"[^>]*>(.*?)</span>', inner, re.S)
                heading = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", inner, re.S)
                out.append(_job(m.group(2), heading.group(1) if heading else inner,
                                f"https://{entry['host']}{m.group(1)}", loc.group(1) if loc else ""))
            pages = re.search(r'data-total-pages="(\d+)"', frag)
            if not fresh or page >= int(pages.group(1) if pages else 1):
                break
    return out


def _radancy_body(page):
    desc = _ld_description(page)
    return desc if desc else _main(page)


def _text_radancy(entry, job, http):
    return text_of(_radancy_body(_get(http, job["url"])))


# --- Avature (EA): HTML search results ---------------------------------------------------

_AVATURE_ITEM = re.compile(r'<a[^>]+href="([^"]*/JobDetail/[^"/]+/(\d+))"[^>]*>(.*?)</a>', re.S)


def _list_avature(entry, http):
    out = []
    per_page = 100
    for q in entry["queries"]:
        seen, offset = set(), 0
        for _ in range(MAX_PAGES):
            qs = urlencode({"listFilterMode": 1, "jobRecordsPerPage": per_page, "jobOffset": offset})
            page = _get(http, f"https://{entry['host']}/en_US/careers/SearchJobs/{quote(q)}?{qs}")
            blocks = page.split("<article")[1:]
            fresh = 0
            for b in blocks:
                m = _AVATURE_ITEM.search(b)
                if not m or m.group(2) in seen:
                    continue
                seen.add(m.group(2))
                fresh += 1
                loc = re.search(r'class="list-item-location"[^>]*>(.*?)</span>', b, re.S)
                out.append(_job(m.group(2), m.group(3), urljoin(f"https://{entry['host']}/", m.group(1)),
                                loc.group(1) if loc else ""))
            if not fresh or len(blocks) < per_page:
                break
            offset += per_page
    return out


def _text_avature(entry, job, http):
    return text_of(_main(_get(http, job["url"])))


# --- Amazon ----------------------------------------------------------------------------

def _amazon_page(http, query, offset):
    qs = urlencode({"base_query": query, "result_limit": 100, "offset": offset, "sort": "relevant"})
    return _get_json(http, f"https://www.amazon.jobs/en/search.json?{qs}")


def _list_amazon(entry, http):
    out = []
    for q in entry["queries"]:
        offset = 0
        while True:
            d = _amazon_page(http, q, offset)
            jobs = d.get("jobs") or []
            out += [_job(j["id_icims"], j["title"], "https://www.amazon.jobs" + j["job_path"],
                         j.get("normalized_location") or j.get("location")) for j in jobs]
            offset += 100
            if not jobs or offset >= (d.get("hits") or 0):
                break
    return out


def _text_amazon(entry, job, http):
    for j in _amazon_page(http, job["id"], 0).get("jobs") or []:
        if str(j["id_icims"]) == job["id"]:
            return _parts(j.get("description"), j.get("basic_qualifications"), j.get("preferred_qualifications"))
    raise FeedError(f"Amazon posting {job['id']} not found")


# --- Google: job data is embedded in the results page --------------------------------------

def _google_entries(page):
    """Yield (id, title, location, raw record) for each ["<id>","<title>",...] record in the page data."""
    end = 0
    for m in re.finditer(r'\["(\d{15,20})","([^"\\]|\\.)*"', page):
        if m.start() < end:
            continue
        end = _match_bracket(page, m.start())
        try:
            rec = json.loads(page[m.start():end])
            loc = rec[9][0][0]
        except (ValueError, IndexError, TypeError, KeyError):
            rec, loc = None, ""
        if rec:
            yield m.group(1), rec[1], loc or "", page[m.start():end]


def _list_google(entry, http):
    out = []
    for q in entry["queries"]:
        seen = set()
        for page in range(1, MAX_PAGES + 1):
            html_ = _get(http, "https://www.google.com/about/careers/applications/jobs/results?"
                               + urlencode({"q": q, "page": page}))
            fresh = [e for e in _google_entries(html_) if e[0] not in seen]
            for id_, title, loc, _ in fresh:
                seen.add(id_)
                out.append(_job(id_, title, "https://www.google.com/about/careers/applications/jobs/results/"
                                            f"{id_}-{_slug(title)}", loc))
            if len(fresh) < 20:
                break
    return out


def _google_body(page):
    m = re.search(r'(?s)<h2 class="p1N2lc">.*?(?=<h2 class="d3Kuye">|$)', page)
    return m.group(0) if m else page


def _text_google(entry, job, http):
    return text_of(_google_body(_get(http, job["url"])))


# --- Apple -----------------------------------------------------------------------------------

_APPLE_ITEM = re.compile(r'<a\s[^>]*href="/en-us/details/(\d+)(?:-\d+)?/([^"?]+)[^"]*"[^>]*>(.*?)</a>', re.S)


def _list_apple(entry, http):
    out = []
    for q in entry["queries"]:
        seen = set()
        for page in range(1, MAX_PAGES + 1):
            body = _get(http, "https://jobs.apple.com/en-us/search?" + urlencode({"search": q, "page": page}))
            items = list(_APPLE_ITEM.finditer(body))
            fresh = 0
            for n, m in enumerate(items):
                title = text_of(m.group(3))
                if m.group(1) in seen or not title:
                    continue
                seen.add(m.group(1))
                fresh += 1
                chunk = body[m.end(): items[n + 1].start() if n + 1 < len(items) else len(body)]
                loc = re.search(r'id="search-store-name-container[^"]*"[^>]*>(.*?)</span>', chunk, re.S)
                out.append(_job(m.group(1), title, f"https://jobs.apple.com/en-us/details/{m.group(1)}/{m.group(2)}",
                                loc.group(1) if loc else ""))
            if not fresh:
                break
    return out


def _apple_details(page):
    m = re.search(r'__staticRouterHydrationData\s*=\s*JSON\.parse\("(.*?)"\);', page, re.S)
    if not m:
        return None
    try:
        return json.loads(json.loads('"' + m.group(1) + '"'))["loaderData"]["jobDetails"]["jobsData"]
    except (ValueError, KeyError, TypeError):
        return None


def _text_apple(entry, job, http):
    page = _get(http, job["url"])
    d = _apple_details(page)
    if not d:
        return text_of(page)
    return _parts(*(d.get(k) for k in ("jobSummary", "description", "responsibilities",
                                       "minimumQualifications", "preferredQualifications")))


# --- TikTok ------------------------------------------------------------------------------------

TIKTOK_HEADERS = {"website-path": "tiktok", "Origin": "https://lifeattiktok.com"}


def _tiktok_page(http, keyword, offset):
    return _post_json(http, "https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts",
                      {"keyword": keyword, "limit": 50, "offset": offset, "recruitment_id_list": [],
                       "job_category_id_list": [], "subject_id_list": [], "location_code_list": []},
                      TIKTOK_HEADERS)["data"]


def _tiktok_pages(http, queries):
    for q in queries:
        offset = 0
        for _ in range(MAX_PAGES * 4):
            d = _tiktok_page(http, q, offset)
            posts = d.get("job_post_list") or []
            yield posts
            offset += 50
            if not posts or offset >= (d.get("count") or 0):
                break


def _list_tiktok(entry, http):
    return [_job(p["id"], p["title"], f"https://lifeattiktok.com/search/{p['id']}",
                 (p.get("city_info") or {}).get("en_name"))
            for posts in _tiktok_pages(http, entry["queries"]) for p in posts]


def _text_tiktok(entry, job, http):
    # The API has no by-id lookup (and search ignores ids), so scan the watchlist queries.
    for posts in _tiktok_pages(http, entry["queries"]):
        for p in posts:
            if p["id"] == job["id"]:
                return _parts(p.get("description"), p.get("requirement"))
    raise FeedError(f"TikTok posting {job['id']} not found")


# --- IBM -----------------------------------------------------------------------------------------

def _ibm_search(http, query, fields, source, offset=0, size=100):
    return _post_json(http, "https://www-api.ibm.com/search/api/v2",
                      {"appId": "careers", "scopes": ["careers2"],
                       "query": {"bool": {"must": [{"simple_query_string": {"query": query, "fields": fields}}]}},
                       "size": size, "from": offset, "lang": "zz", "localLang": "xx", "_source": source})["hits"]


def _ibm_id(url):
    m = re.search(r"jobId=(\d+)", url)
    return m.group(1) if m else url


def _list_ibm(entry, http):
    out = []
    for q in entry["queries"]:
        offset = 0
        while True:
            h = _ibm_search(http, q, ["title^3", "body^1", "description^2", "keywords^1"], ["title", "url"], offset)
            hits = h.get("hits") or []
            out += [_job(_ibm_id(x["_source"]["url"]), x["_source"]["title"], x["_source"]["url"]) for x in hits]
            offset += 100
            if not hits or offset >= (h.get("total") or {}).get("value", 0):
                break
    return out


def _text_ibm(entry, job, http):
    h = _ibm_search(http, job["id"], ["url"], ["title", "url", "description", "body"], size=10)
    for x in h.get("hits") or []:
        if _ibm_id(x["_source"]["url"]) == job["id"]:
            return _parts(x["_source"].get("description"), x["_source"].get("body"))
    raise FeedError(f"IBM posting {job['id']} not found")


# --- Atlassian: every posting in one call -----------------------------------------------------------

def _atlassian_all(http):
    return _get_json(http, "https://www.atlassian.com/endpoint/careers/listings")


def _list_atlassian(entry, http):
    return [_job(j["id"], j["title"], f"https://www.atlassian.com/company/careers/details/{j['id']}",
                 "; ".join(j.get("locations") or [])) for j in _atlassian_all(http)]


def _text_atlassian(entry, job, http):
    for j in _atlassian_all(http):
        if str(j["id"]) == job["id"]:
            return _parts(j.get("overview"), j.get("responsibilities"), j.get("qualifications"))
    raise FeedError(f"Atlassian posting {job['id']} not found")


# --- dispatch -------------------------------------------------------------------------------------------

_ADAPTERS = {
    "greenhouse": (_list_greenhouse, _text_greenhouse),
    "ashby": (_list_ashby, _text_ashby),
    "lever": (_list_lever, _text_lever),
    "workday": (_list_workday, _text_workday),
    "oracle": (_list_oracle, _text_oracle),
    "eightfold": (_list_eightfold, _text_eightfold),
    "jibe": (_list_jibe, _text_jibe),
    "radancy": (_list_radancy, _text_radancy),
    "avature": (_list_avature, _text_avature),
    "amazon": (_list_amazon, _text_amazon),
    "google": (_list_google, _text_google),
    "apple": (_list_apple, _text_apple),
    "tiktok": (_list_tiktok, _text_tiktok),
    "ibm": (_list_ibm, _text_ibm),
    "atlassian": (_list_atlassian, _text_atlassian),
}
_SHAPE_ERRORS = (KeyError, IndexError, TypeError, AttributeError)


def list_jobs(entry, http=fetch):
    """Every posting the company's feed returns for the entry's queries, deduped by id.

    Raises FeedError on an HTTP error, an unreadable response, or when the feed
    returns nothing at all (before any filtering)."""
    try:
        raw = _ADAPTERS[entry["ats"]][0](entry, http)
    except _SHAPE_ERRORS as e:
        raise FeedError(f"{entry['company']}: unexpected response shape ({e!r})")
    if not raw:
        raise FeedError(f"{entry['company']}: feed returned 0 results")
    seen, out = set(), []
    for j in raw:
        if j["id"] not in seen:
            seen.add(j["id"])
            out.append(j)
    return out


def posting_text(entry, job, http=fetch):
    """Plain text of one posting. `job` needs only "id" and "url"."""
    try:
        return _ADAPTERS[entry["ats"]][1](entry, job, http)
    except _SHAPE_ERRORS as e:
        raise FeedError(f"{entry['company']}: unexpected response shape ({e!r})")


# --- recording fixtures (--record) ---------------------------------------------------------------------------

# Where each JSON list endpoint keeps its jobs, and the totals to rewrite after trimming.
_JSON_LISTS = {
    "greenhouse": (("jobs",), ()),
    "ashby": (("jobs",), ()),
    "lever": ((), ()),
    "workday": (("jobPostings",), (("total",),)),
    "oracle": (("items", 0, "requisitionList"), (("items", 0, "TotalJobsCount"),)),
    "eightfold": (("data", "positions"), (("data", "count"),)),
    "jibe": (("jobs",), (("totalCount",), ("count",))),
    "amazon": (("jobs",), (("hits",),)),
    "tiktok": (("data", "job_post_list"), (("data", "count"),)),
    "ibm": (("hits", "hits"), (("hits", "total", "value"),)),
    "atlassian": ((), ()),
}
EXTENSIONS = {a: ("json", "json") for a in _JSON_LISTS}
EXTENSIONS.update({"radancy": ("json", "html"), "avature": ("html", "html"),
                   "google": ("html", "html"), "apple": ("html", "html")})
# Detail responses that are a single job but carry bulky extras.
_DROP_KEYS = ("similarJobs", "hiringOrganization")


def _path_get(obj, path):
    for k in path:
        obj = obj[k]
    return obj


def _trim_json(ats, text, n):
    obj = json.loads(text)
    path, totals = _JSON_LISTS[ats]
    if path:
        parent = _path_get(obj, path[:-1])
        parent[path[-1]] = parent[path[-1]][:n]
    else:
        obj = obj[:n]
    for t in totals:
        parent = _path_get(obj, t[:-1])
        parent[t[-1]] = n
    return json.dumps(obj, indent=1, ensure_ascii=False)


def _trim_blocks(text, pattern, n):
    """Keep the head of `text`, the first n matches of pattern, and the tail after the last match."""
    ms = list(re.finditer(pattern, text, re.S))
    if len(ms) <= n:
        return text
    return text[:ms[n].start()] + text[ms[-1].end():]


def _trim_list(ats, text, n=3):
    if ats in _JSON_LISTS:
        return _trim_json(ats, text, n)
    if ats == "radancy":
        d = json.loads(text)
        d["results"] = _trim_blocks(d["results"], r"<li\b.*?</li>", n)
        d["filters"] = ""
        return json.dumps(d, indent=1, ensure_ascii=False)
    if ats == "avature":
        return _trim_blocks(text, r"<article\b.*?</article>", n)
    if ats == "apple":
        items = re.finditer(r'<li [^>]*>(?=<div id="title-search-job-list-).*?</li>', text, re.S)
        return "<html><body><ul>" + "\n".join(m.group(0) for m in list(items)[:n]) + "</ul></body></html>"
    # google: keep the first n records, as a bare array inside a script tag
    recs = [e[3] for e in _google_entries(text)][:n]
    return "<html><body><script>[" + ",".join(recs) + "]</script></body></html>"


def _trim_detail(ats, text):
    if ats in ("ashby", "jibe", "amazon", "tiktok", "ibm", "atlassian"):
        return _trim_json(ats, text, 3)  # the lookup is a search or the whole board
    if ats in ("greenhouse", "lever", "workday", "oracle", "eightfold"):
        obj = json.loads(text)
        for k in _DROP_KEYS:
            obj.pop(k, None)
        return json.dumps(obj, indent=1, ensure_ascii=False)
    if ats == "radancy":
        desc = _ld_description(text)
        ld = json.dumps({"@type": "JobPosting", "description": desc}, ensure_ascii=False)
        return f'<html><body><script type="application/ld+json">{ld}</script></body></html>'
    if ats == "avature":
        return f"<html><body>{_main(text)}</body></html>"
    if ats == "google":
        return f"<html><body>{_google_body(text)}</body></html>"
    d = _apple_details(text)  # apple: keep only the posting data, same wrapper
    wrapped = json.dumps(json.dumps({"loaderData": {"jobDetails": {"jobsData": d}}}))[1:-1]
    return f'<html><body><script>window.__staticRouterHydrationData = JSON.parse("{wrapped}");</script></body></html>'


def record(company, entries=None, out_dir=FIXTURES):
    """Hit the live feed for `company` and write trimmed list/detail fixtures for its ATS."""
    entries = entries if entries is not None else load_watchlist()
    entry = next((e for e in entries if e["company"] == company), None)
    if entry is None:
        raise SystemExit(f"no watchlist entry for {company!r}")
    calls = []

    def spy(url, method="GET", body=None, headers=None):
        status, text = fetch(url, method, body, headers)
        if status == 200:  # a 429 retry must not become the fixture
            calls.append((status, text))
        return status, text

    jobs = list_jobs(entry, http=spy)
    list_raw, calls[:] = calls[0][1], []
    time.sleep(EIGHTFOLD_DELAY + 1)  # polite to every feed, required by Eightfold
    posting_text(entry, jobs[0], http=spy)
    detail_raw = calls[0][1]
    ats = entry["ats"]
    ext_list, ext_detail = EXTENSIONS[ats]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = (out_dir / f"{ats}_list.{ext_list}", out_dir / f"{ats}_detail.{ext_detail}")
    paths[0].write_text(_trim_list(ats, list_raw) + "\n", encoding="utf-8")
    paths[1].write_text(_trim_detail(ats, detail_raw) + "\n", encoding="utf-8")
    return paths, jobs[0]


def main(argv):
    if len(argv) == 2 and argv[0] == "--record":
        paths, first = record(argv[1])
        print(f"first job: {first}")
        for p in paths:
            print(f"wrote {p} ({p.stat().st_size} bytes)")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
