"""
Upwork jobs -> sharten -> AI (apply / review / skip) -> Slack
Data source: Apify actor hyperbach/upwork-scraper-ai (jobs ~1-4 minute mein)

Niche (v8): Ecommerce / Shopify operations & management (ops, store manager, project manager,
CS manager, 3PL / fulfillment), phir Shopify development (redesign, setup, migration) aur automation / AI.

Sharten (sirf yehi):
  - (title mein LIST_A aur description mein LIST_B) ya (title mein LIST_B aur description mein LIST_A)
  - Hourly: kam az kam $5 (range ka max)       -> GitHub variable MIN_HOURLY
  - Fixed: kam az kam $150                      -> GitHub variable MIN_FIXED
  - Client hire rate 40% ya zyada (sirf jab 10 se zyada jobs post ki hon, naya client theek)
  - Client total spent / total hires $50 se zyada (0 hire wala naya client theek)
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from apify_client import ApifyClient

# GitHub log mein hamari lines foran aur sahi tarteeb mein aayein
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# ---------- Secrets ----------
APIFY_TOKEN = os.environ["APIFY_TOKEN"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
# Sleep / wake (Slack mein "sleep" ya "wake" likhein). Token na ho to ye feature band, system hamesha chalu.
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN") or ""
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID") or ""
SLEEP_AT = os.getenv("SLEEP_AT") or "06:00"     # Pakistan time, roz khud sleep
WAKE_AT = os.getenv("WAKE_AT") or "12:00"       # Pakistan time, roz khud wake
AI_API_KEY = os.environ["AI_API_KEY"]
AI_BASE_URL = os.getenv("AI_BASE_URL") or "https://api.openai.com/v1"
AI_MODEL = os.getenv("AI_MODEL") or "gpt-5.4"


def env_bool(name, default):
    v = (os.getenv(name) or "").strip().lower()
    return default if v == "" else v in ("1", "true", "yes", "on")


# =====================================================================
#  CONFIG: sab keywords aur hadein yahan (edit karna ho to sirf ye hissa)
# =====================================================================
# LIST_A: ecommerce context
LIST_A = [
    r"shopify\w*", r"shopline", r"e[\s\-]?commerce", r"ecom", r"online[\s\-]?stores?",
    r"dtc", r"d2c", r"dropship\w*", r"stores?",
]
# LIST_B: role / kaam
LIST_B = [
    r"operations?", r"operators?", r"ops", r"back[\s\-]?end", r"manag\w*", r"team",
    r"project managers?", r"right[\s\-]?hand", r"coo", r"head of", r"lead(?![\s\-]?gen)",
    r"store managers?", r"customer (?:service|support|experience|care|success)", r"support",
    r"virtual assistants?", r"va", r"assistants?", r"admin\w*",
    r"product listings?", r"product uploads?", r"listings?", r"catalog\w*",
    r"order processing", r"order management", r"fulfil+ment", r"inventory",
    r"automat\w*", r"ai", r"claude", r"chat ?gpt", r"google sheets?", r"apps? ?script",
    r"redesign\w*", r"store (?:setup|set up|build\w*)", r"migrat\w*", r"experts?",
    # roz ka kaam / ops
    r"daily tasks?", r"day[\s\-]to[\s\-]day", r"store (?:maintenance|updates?|tasks?)", r"orders?",
    r"shipping", r"returns?", r"refunds?", r"chargebacks?", r"suppliers?", r"sourcing", r"3pl", r"logistics",
    r"coordinat\w*", r"specialists?", r"generalists?", r"directors?", r"executives?",
    r"sops?", r"kpis?", r"reporting", r"dashboards?", r"workflows?", r"processes",
    r"collections?", r"merchandis\w*", r"promotions?", r"launch\w*",
]
# Code score (GPT se pehle)
SCORE_OPS_WORDS = [r"team", r"suppliers?", r"3pl", r"fulfil+ment", r"inventory", r"sops?", r"kpis?",
                   r"weekly reports?", r"refunds?", r"escalations?", r"hiring", r"training"]
SCORE_LONG_WORDS = [r"long[\s\-]?term", r"ongoing", r"contract[\s\-]to[\s\-]hire", r"more than 6 months",
                    r"6\+ months", r"full[\s\-]?time", r"permanent"]

MIN_HOURLY = int(os.getenv("MIN_HOURLY") or "5")           # hourly range ka max is se kam = skip
MIN_FIXED = int(os.getenv("MIN_FIXED") or "150")           # fixed budget kam az kam
MIN_HIRE_RATE = 40        # hire rate 40% ya zyada (sirf jab client 10 se zyada jobs post kar chuka ho)
HIRE_RATE_AFTER_JOBS = 10  # is se kam / barabar jobs = naya client, hire rate nahi dekhte
MIN_SPENT_PER_HIRE = 50   # spent / hires is se zyada

# Skip jobs bhi Slack pe (sirf wo jo niche se match hui, baaqi kachra nahi)
SEND_SKIPPED = env_bool("SEND_SKIPPED", True)
# Review: AI apply kahe lekin score is se kam ho to "🟡 Review" label
REVIEW_BELOW = 7

# Apify search (title + description + skills teeno mein). "store" jaan boojh ke nahi (bahut aam lafz)
FEED_KEYWORDS = ('shopify OR shopifyplus OR shopline OR ecommerce OR e-commerce OR ecom OR '
                 '"online store" OR DTC OR D2C OR dropshipping')
# =====================================================================

# ---------- Settings ----------
ACTOR_ID = "hyperbach/upwork-scraper-ai"
FEED_ID = "meesum-v8"                 # naya naam = sirf abhi ke baad ki jobs (purani nahi)
STORE_NAME = "upwork-shopify-state"
LIMIT = 200                           # ek run mein max jobs (safety)
FRESH_MAX_AGE_MIN = 60                # 1 ghante se purani job kabhi nahi

# Backup: Hyperbach se chhooti jobs pakadne ke liye Black Falcon (seedha Upwork live search)
BACKUP_ACTOR = "blackfalcondata/upwork-scraper"
BACKUP_EVERY_MIN = int(os.getenv("BACKUP_EVERY_MIN") or "60")   # 0 = backup band
BACKUP_MAX_AGE_MIN = 60               # backup bhi sirf pichle 1 ghante ki jobs
BACKUP_QUERY = "shopify OR shopifyplus OR shopline"   # tang rakha (backup ka kharcha zyada hai)
BACKFILL_HOURS = int(os.getenv("BACKFILL_HOURS") or "0")
BACKFILL_LIMIT = 300
# Har run ke baad status message: "every_run" (har 5 min), "hourly" (ghante mein ek), "off"
STATUS_MESSAGES = (os.getenv("STATUS_MESSAGES") or "off").lower()

HERE = Path(__file__).parent
CRITERIA = (HERE / "prompt.txt").read_text(encoding="utf-8")


def words_re(items):
    """Poore lafz match karo: 'VA' value mein nahi, 'AI' email mein nahi."""
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(items) + r")(?![A-Za-z0-9])", re.I)


LIST_A_RE = words_re(LIST_A)
LIST_B_RE = words_re(LIST_B)
OPS_WORDS_RE = words_re(SCORE_OPS_WORDS)
LONG_RE = words_re(SCORE_LONG_WORDS)
MONTHLY_RE = re.compile(r"\$\s?([\d,]{3,7})(?:\.\d+)?\s*(?:usd\s*)?(?:/|per|a)\s*(?:month|mo)\b", re.I)

# Angle tags (pehla jo match ho, isi tarteeb mein)
TAG_RULES = [
    ("[OPS]", words_re([r"operations?", r"operators?", r"ops", r"back[\s\-]?end", r"head of", r"coo",
                        r"right[\s\-]?hand"])),
    ("[PM]", words_re([r"project managers?", r"project management"])),
    ("[STORE]", words_re([r"store managers?", r"shopify managers?", r"e[\s\-]?commerce managers?",
                          r"ecom managers?", r"store management"])),
    ("[CS-MGR]", re.compile(r"(?=.*\bcustomer (?:service|support|experience|care|success)\b)"
                            r"(?=.*\b(?:managers?|lead|head)\b)", re.I | re.S)),
    ("[AUTO]", words_re([r"automat\w*", r"ai", r"claude", r"chat ?gpt", r"google sheets?", r"apps? ?script"])),
    ("[DEV]", words_re([r"redesign\w*", r"store (?:setup|set up|build\w*)", r"migrat\w*", r"experts?",
                        r"shopify developer", r"developer", r"theme", r"liquid"])),
    ("[VA/CS]", words_re([r"virtual assistants?", r"va", r"assistants?", r"customer (?:service|support|care)",
                          r"support", r"product listings?", r"product uploads?", r"listings?"])),
]
TAGS = [t for t, _ in TAG_RULES]


# ---------- Helpers ----------
def field(obj, *names):
    """Apify library ke purane (dict) aur naye (object) dono versions ke liye."""
    if obj is None:
        return None
    for n in names:
        if isinstance(obj, dict):
            if n in obj:
                return obj[n]
        elif hasattr(obj, n):
            return getattr(obj, n)
    return None


def run_ok(run):
    return "SUCCEEDED" in str(field(run, "status") or "")


def parse_time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def minutes_ago(s):
    t = parse_time(s)
    if not t:
        return None
    return max(0, int((datetime.now(timezone.utc) - t).total_seconds() // 60))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- Job ko ek shape mein lao ----------
def normalize(row):
    price_type = str(row.get("price_type") or "")
    skills = row.get("skills") or ""
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",") if s.strip()]
    qs = []
    for q in row.get("questions") or []:
        qs.append(q if isinstance(q, str) else (q.get("question") or q.get("text") or str(q)))
    duration = " ".join(str(row.get(k) or "") for k in ("engagement_label", "engagement_duration",
                                                         "duration", "ai_duration")).strip()
    return {
        "uid": str(row.get("id") or row.get("url") or ""),
        "title": row.get("title") or "Untitled",
        "description": row.get("description") or "",
        "url": row.get("url") or "",
        "kind": "fixed" if price_type.lower().startswith("fixed") else "hourly",
        "fixed_budget": num(row.get("price")),
        "hourly_min": num(row.get("price_min")),
        "hourly_max": num(row.get("price_max")),
        "skills": skills,
        "experience": row.get("experience_level") or "?",
        "duration": duration,
        "country": row.get("client_location") or "?",
        "payment_verified": bool(row.get("buyer_payment_verified")),
        "hire_rate": num(row.get("hire_rate")),
        "hires": num(row.get("hires")),
        "jobs_posted": num(row.get("jobs_posted")),
        "spent": num(row.get("total_spent")),
        "rating": row.get("buyer_score"),
        "applicants": row.get("client_total_applicants"),
        "questions": qs,
        "allowed_countries": row.get("qual_countries"),
        "posted": row.get("date_posted"),
        "source": row.get("_source") or "feed",
    }


def body_text(job):
    """'Description' = description + skills (dono mein dhoondo)."""
    return " ".join([job.get("description") or "", " ".join(job.get("skills") or [])])


# ---------- Sharten ----------
def keyword_match(job):
    """(title A + desc B) ya (title B + desc A)."""
    title, desc = job["title"], body_text(job)
    ta, tb = bool(LIST_A_RE.search(title)), bool(LIST_B_RE.search(title))
    da, db = bool(LIST_A_RE.search(desc)), bool(LIST_B_RE.search(desc))
    return (ta and db) or (tb and da)


def check(job):
    """Return (ok, info, niche_match). niche_match False = kachra, Slack pe bhi nahi."""
    if not keyword_match(job):
        return False, "keyword rule match nahi", False
    hire_rate = job["hire_rate"] or 0
    posted_jobs = job.get("jobs_posted") or 0
    if posted_jobs > HIRE_RATE_AFTER_JOBS and hire_rate < MIN_HIRE_RATE:   # naya client theek
        return False, f"hire rate {hire_rate:.0f}% ({posted_jobs:.0f} jobs posted)", True
    hires, spent = job["hires"] or 0, job["spent"] or 0
    per_hire = spent / hires if hires else 0
    if hires > 0 and per_hire <= MIN_SPENT_PER_HIRE:                       # 0 hire wala naya client theek
        return False, f"${per_hire:.0f}/hire", True
    if job["kind"] == "hourly":
        top = job["hourly_max"] or job["hourly_min"]
        if top is not None and top < MIN_HOURLY:      # rate likha hi nahi to theek
            return False, f"hourly ${top:.0f} se kam", True
    if job["kind"] == "fixed" and (job["fixed_budget"] or 0) < MIN_FIXED:
        return False, f"fixed ${job['fixed_budget'] or 0:,.0f} (kam az kam ${MIN_FIXED})", True
    return True, {"per_hire": per_hire}, True


def code_score(job):
    """GPT se pehle apna score (max 10) aur wajahein."""
    title, desc = job["title"], body_text(job)
    pts, why = 0, []
    if LIST_A_RE.search(title) and LIST_B_RE.search(title):
        pts += 3; why.append("title +3")
    if OPS_WORDS_RE.search(desc):
        pts += 2; why.append("ops kaam +2")
    monthly = max([num(x.replace(",", "")) or 0 for x in MONTHLY_RE.findall(desc)] or [0])
    if (job["kind"] == "hourly" and (job["hourly_max"] or 0) >= 20) or monthly >= 1500:
        pts += 2; why.append("rate +2")
    if LONG_RE.search(f"{title} {desc} {job['duration']}"):
        pts += 2; why.append("long term +2")
    if (job["spent"] or 0) >= 10000:
        pts += 1; why.append("$10K+ client +1")
    return min(pts, 10), why


def angle_tag(job):
    for text in (job["title"], body_text(job)[:1500]):
        for tag, rx in TAG_RULES:
            if rx.search(text):
                return tag
    return "[OPS]"


def money_text(job):
    if job["kind"] == "fixed":
        return f"Fixed ${job['fixed_budget'] or 0:,.0f}"
    lo, hi = job["hourly_min"], job["hourly_max"]
    if lo and hi and lo != hi:
        return f"Hourly ${lo:,.0f}-${hi:,.0f}/hr"
    if lo or hi:
        return f"Hourly ${(lo or hi):,.0f}/hr"
    return "Hourly (rate not given)"


# ---------- AI ----------
def job_to_text(job, info):
    lines = [
        f"Title: {job['title']}",
        f"Budget: {money_text(job)}",
        f"Duration: {job['duration'] or '?'}",
        f"Client: {job['country']}, {job.get('jobs_posted') or 0:.0f} jobs posted, spent ${job['spent'] or 0:,.0f}, "
        f"{job['hires'] or 0:.0f} hires, "
        f"hire rate {job['hire_rate'] or 0:.0f}%, rating {job['rating'] or '?'}",
        f"Skills: {', '.join(job['skills'])}",
        f"Experience level: {job['experience']}",
        f"Code pre-score: {info['code_score']}/10 ({', '.join(info['code_why']) or 'no bonus'})",
        f"Suggested angle tag: {info['tag']}",
    ]
    desc = job["description"]
    if len(desc) > 3000:
        desc = desc[:3000] + " ..."
    lines += ["", "Description:", desc]
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You decide whether a freelancer should apply to an Upwork job. You do NOT write proposals.

## Freelancer's criteria for deciding whether to apply:
{CRITERIA}

Respond ONLY with a JSON object, no other text:
{{"apply": true or false,
  "score": integer 0-10 (how good a fit),
  "summary": "2 to 3 short sentences in ROMAN URDU (Urdu written in English letters, e.g. 'Client ko Shopify store ke liye operations manager chahiye...'): client ko kya chahiye, asal kaam kya hain, aur koi deadline, tool ya khaas shart",
  "reason": "start with ONE angle tag ({', '.join(TAGS)}), then a very short reason, max 12 words",
  "risk": "one short warning if something looks risky (vague scope, tiny budget, unrealistic deadline, off-platform contact), otherwise empty string"}}"""


def ask_ai(job_text):
    r = requests.post(
        f"{AI_BASE_URL.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {AI_API_KEY}"},
        json={"model": AI_MODEL,
              "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": job_text}],
              "response_format": {"type": "json_object"}},
        timeout=120,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"].strip()
    content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(content)


def fix_reason(reason, tag):
    """Reason hamesha kisi sahi tag se shuru ho."""
    reason = str(reason or "").strip()
    if any(reason.upper().startswith(t) for t in TAGS):
        return reason
    reason = re.sub(r"^\[[^\]]*\]\s*", "", reason)     # purana / ghalat tag hatao
    return f"{tag} {reason}".strip()


# ---------- Slack ----------
def esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_post(payload):
    r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=20)
    if not r.ok:
        print("Slack error:", r.status_code, r.text)
    r.raise_for_status()
    time.sleep(1.1)  # Slack webhook limit: 1 message/second


def slack_text(text):
    slack_post({"text": text[:39000], "mrkdwn": False})


def slack_job(lines, url=None):
    body = "\n".join(lines)
    if url:
        body += f"\n<{url}|{esc(url)}>"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}}]
    if url:
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "style": "primary",
            "text": {"type": "plain_text", "text": "🔗 Open Job"},
            "url": url, "action_id": "open_job"}]})
    slack_post({"text": lines[0] if lines else "Upwork job", "blocks": blocks})


PKT = timezone(timedelta(hours=5))


def posted_text(job):
    t = parse_time(job["posted"])
    if not t:
        return None
    ago = minutes_ago(job["posted"])
    local = t.astimezone(PKT)
    stamp = f"{local.day} {local.strftime('%b')}, {local.strftime('%I:%M %p').lstrip('0')}"
    return f"⏱ Posted {ago} min ago ({stamp})"


def client_line(job):
    hires, spent = job["hires"] or 0, job["spent"] or 0
    per_hire = spent / hires if hires else 0
    return (f"{job.get('jobs_posted') or 0:.0f} jobs posted  |  ${spent:,.0f} spent  |  {hires:.0f} hires (${per_hire:,.0f}/hire)  |  "
            f"{job['hire_rate'] or 0:.0f}% hire rate")


def extra_line(job):
    extra = [posted_text(job)]
    if job.get("source") == "backup":
        extra.append("🛟 Backup")
    if job["applicants"] is not None:
        extra.append(f"👥 {job['applicants']} proposals")
    return "  |  ".join(e for e in extra if e)


def job_lines(job, info, ai, label):
    lines = [
        f"{label} *{esc(job['title'])}*",
        f"💰 {esc(money_text(job))}  |  ⭐ AI {ai.get('score', '?')}/10  |  🧮 Code {info['code_score']}/10  |  "
        f"🏢 {esc(job['country'])}",
        client_line(job),
        extra_line(job),
    ]
    if ai.get("summary"):
        lines.append(f"📝 {esc(ai['summary'])}")
    lines.append(f"🧠 {esc(fix_reason(ai.get('reason'), info['tag']))}")
    if str(ai.get("risk") or "").strip():
        lines.append(f"⚠️ {esc(ai['risk'])}")
    return lines


def apply_label(ai):
    try:
        score = int(ai.get("score"))
    except (TypeError, ValueError):
        score = 10
    return "🟡 Review:" if score < REVIEW_BELOW else "✅ Apply:"


def notify(job, info, ai):
    slack_job(job_lines(job, info, ai, apply_label(ai)), job["url"])


def notify_skipped(job, info, ai):
    slack_job(job_lines(job, info, ai, "⏭ Skipped (AI):"), job["url"])


def notify_rejected(job, reason):
    """Niche se match hui lekin sharton (budget / client / skip list) pe kati."""
    lines = [
        f"🚫 Skipped (sharten): *{esc(job['title'])}*",
        f"💰 {esc(money_text(job))}  |  🏢 {esc(job['country'])}",
        client_line(job),
        extra_line(job),
        f"❌ Wajah: {esc(reason)}",
    ]
    slack_job(lines, job["url"])


# ---------- Main ----------
def fetch_jobs(client, feed_start):
    base = {
        # Server pe hi chhaant (reject hui jobs ke paise nahi lagte)
        "keywords": FEED_KEYWORDS,           # title + description + skills mein LIST_A
        "keywords_match": "strict",          # "matlab se" match nahi, sirf asal lafz
        "whats_new": False,
    }
    # Kharcha bachao: hire rate aur budget bhi Apify pe hi chhaanto (4 runs)
    hire_parts = {"hr40": {"hire_rate": f">={MIN_HIRE_RATE}"},                  # achhe purane clients
                  "new": {"jobs_posted": f"<={HIRE_RATE_AFTER_JOBS}"}}          # naye clients (10 ya kam jobs)
    price_parts = {"hourly": {"price_type": "Hourly", "price_max": f">={MIN_HOURLY}",
                              "include_unspecified_rate": "all"},
                   "fixed": {"price_type": "Fixed-price", "price": f">={MIN_FIXED}"}}
    if BACKFILL_HOURS > 0:
        print(f"BACKFILL: pichle {BACKFILL_HOURS} ghante ki jobs")
    jobs_in = []
    for hname, hfilt in hire_parts.items():
        for pname, pfilt in price_parts.items():
            name = f"ecom-{hname}-{pname}"
            run_input = {**base, **hfilt, **pfilt, "clientId": f"{FEED_ID}-{name}"}
            if BACKFILL_HOURS > 0:
                run_input.update({"notifications_only": False, "date_posted": f"{BACKFILL_HOURS}h",
                                  "limit": BACKFILL_LIMIT})
            else:
                run_input.update({"notifications_only": True, "limit": LIMIT,
                                  "created_at": f">={feed_start}"})
            jobs_in.append((name, run_input))

    # Saari runs ek saath (taake run jaldi khatam ho)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda x: (x[0], run_actor(client, ACTOR_ID, x[1])), jobs_in))

    rows, ids = [], set()
    for name, got in results:
        print(f"Feed '{name}': {len(got)} jobs")
        for r in got:
            rid = str(r.get("id"))
            if rid not in ids:
                ids.add(rid)
                rows.append(r)
    return rows


def run_actor(client, actor_id, run_input, timeout=240):
    try:
        # Apify actor ka lamba log GitHub mein na aaye (sirf hamari saaf lines)
        run = client.actor(actor_id).call(run_input=run_input, timeout_secs=timeout, logger=None)
    except TypeError:
        run = client.actor(actor_id).call(run_input=run_input, timeout_secs=timeout)
    if not run_ok(run):
        raise RuntimeError(f"{actor_id} run fail: {field(run, 'status')}")
    dataset_id = field(run, "defaultDatasetId", "default_dataset_id")
    items = field(client.dataset(dataset_id).list_items(), "items") or []
    return [i if isinstance(i, dict) else (i.model_dump() if hasattr(i, "model_dump") else dict(i))
            for i in items]


def upwork_id(row):
    """Har scraper ki job ID ek hi shakal mein: ~ ke baad wala hissa (02...)."""
    for key in ("url", "portalUrl", "externalLink"):
        m = re.search(r"~(0\d{10,})", str(row.get(key) or ""))
        if m:
            return m.group(1)
    jid = str(row.get("jobId") or row.get("id") or "")
    if jid.isdigit():
        return jid if jid.startswith("02") else "02" + jid
    return None


def backup_prefilter(row):
    """Black Falcon ke data pe pehli chhalni, taake sirf kaam ki jobs ka poora data mangwayein."""
    skills = row.get("skills") or []
    if isinstance(skills, str):
        skills = [x.strip() for x in skills.split(",")]
    mini = {"title": row.get("title") or "", "skills": skills,
            "description": row.get("description") or row.get("descriptionMarkdown") or ""}
    if not keyword_match(mini):
        return False
    if "FIXED" in str(row.get("jobType") or "").upper():
        budget = num(row.get("budgetAmount"))
        if budget is not None and budget < MIN_FIXED:
            return False
    return True


def fetch_backup(client, known_ids, max_age=BACKUP_MAX_AGE_MIN):
    """Black Falcon se nayi jobs, phir jo chhooti hon un ka poora data Hyperbach se (link ke zariye)."""
    bf_rows = run_actor(client, BACKUP_ACTOR, {
        "query": BACKUP_QUERY,
        "sort": "recency",
        "maxAgeMinutes": max_age + 5,        # thoda overlap, purani job script kaat deti hai
        "maxResults": 100,
        "incrementalMode": True,
        "stateKey": "meesum-backup-v1",
        "descriptionFormat": "text",
    })
    missed = []
    for row in bf_rows:
        jid = upwork_id(row)
        if jid and jid not in known_ids and backup_prefilter(row):
            missed.append(jid)
    missed = list(dict.fromkeys(missed))
    print(f"Backup: Black Falcon ne {len(bf_rows)} jobs di, {len(missed)} chhooti hui lag rahi hain")
    full = []
    for i in range(0, len(missed), 50):
        rows = run_actor(client, ACTOR_ID, {"refresh_job_ids": missed[i:i + 50],
                                            "refresh_shape": "flat", "whats_new": False})
        for r in rows:
            r["_source"] = "backup"
        full += rows
    return full


# ---------- Sleep / wake ----------
SLEEP_MSG = "😴 Sleep mode on"
WAKE_MSG = "☀️ Wake mode on"


def last_schedule_event(now):
    """Pichla auto sleep/wake waqt (Pakistan time) aur us ka mode."""
    events = []
    local_now = now.astimezone(PKT)
    for days_back in (0, 1):
        day = (local_now - timedelta(days=days_back)).date()
        for hhmm, mode in ((SLEEP_AT, "sleep"), (WAKE_AT, "wake")):
            h, m = (int(x) for x in hhmm.split(":"))
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=PKT)
            if t <= local_now:
                events.append((t.astimezone(timezone.utc), mode))
    return max(events)


def slack_history_since(oldest_ts):
    msgs, cursor = [], None
    while True:
        params = {"channel": SLACK_CHANNEL_ID, "oldest": f"{oldest_ts:.6f}", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = requests.get("https://slack.com/api/conversations.history",
                         headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}, params=params, timeout=20)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack read error: {data.get('error')}")
        msgs += data.get("messages", [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return msgs


def control_mode(now):
    """Return ('sleep'|'wake', just_woke). Jo sab se aakhri hua (aap ka hukm ya auto waqt), wahi chalta hai."""
    if not (SLACK_BOT_TOKEN and SLACK_CHANNEL_ID):
        return "wake", False
    sched_time, sched_mode = last_schedule_event(now)
    msgs = slack_history_since(sched_time.timestamp())
    manual = None                                   # aap ka sab se naya "sleep"/"wake"
    for m in msgs:                                  # Slack naye se purane ki tarteeb mein deta hai
        if m.get("bot_id") or m.get("subtype"):
            continue
        cmd = (m.get("text") or "").strip().lower()
        if cmd in ("sleep", "wake"):
            manual = (float(m["ts"]), cmd)
            break
    if manual:
        mode, since_ts, who = manual[1], manual[0], "aap ne likha"
    else:
        mode, since_ts = sched_mode, sched_time.timestamp()
        who = f"auto {(SLEEP_AT if mode == 'sleep' else WAKE_AT)}"
    # Slack emoji ko ":sunny:" jaise code mein badal deta hai, is liye sirf alfaaz dhoondo
    marker = "Sleep mode on" if mode == "sleep" else "Wake mode on"
    announced = any((m.get("bot_id") or m.get("subtype") == "bot_message") and float(m["ts"]) >= since_ts and
                    marker in (m.get("text") or "")
                    for m in msgs)
    just_woke = False
    if not announced:
        if mode == "sleep":
            slack_text(f"{SLEEP_MSG} ({who}). Jobs band, \"wake\" likhein to chalu.")
        else:
            slack_text(f"{WAKE_MSG} ({who}). Jobs chalu.")
            just_woke = True
    return mode, just_woke


def main():
    now = datetime.now(timezone.utc)
    if BACKFILL_HOURS == 0:
        try:
            mode, just_woke = control_mode(now)
        except Exception as ex:
            print("Sleep/wake check error (system chalu rakha):", ex)
            mode, just_woke = "wake", False
        if mode == "sleep":
            print("😴 Sleep mode: Apify call nahi, kharcha nahi")
            return
    else:
        just_woke = False

    client = ApifyClient(APIFY_TOKEN)
    store = client.key_value_stores().get_or_create(name=STORE_NAME)
    kv = client.key_value_store(field(store, "id"))
    state = field(kv.get_record("STATE"), "value") or {}
    seen = state.get("seen", [])
    seen_set = set(seen)

    if just_woke:
        # Sone ke waqt ki jobs ka bill aur dher na aaye: feeds jagne ke waqt se fresh
        state["feed_start"] = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["awake_since"] = now.isoformat()
    if state.get("hb_feed") != FEED_ID or not state.get("feed_start"):
        # Nayi feeds: sirf abhi ke baad ki jobs (purani jobs ka ek saath bill na aaye)
        state["feed_start"] = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = fetch_jobs(client, state["feed_start"])
    print(f"Hyperbach ne kul {len(rows)} nayi jobs di")
    limit = BACKFILL_LIMIT if BACKFILL_HOURS > 0 else LIMIT
    first_feed_run = state.get("hb_feed") != FEED_ID
    if len(rows) >= limit and not (first_feed_run and BACKFILL_HOURS == 0):
        slack_text(f"⚠️ Limit ({limit}) poori ho gayi, kuch jobs miss ho sakti hain.")

    # Backup har BACKUP_EVERY_MIN minute (main feed se chhooti jobs)
    last_backup = parse_time(state.get("backup_last")) if state.get("backup_last") else None
    if (BACKUP_EVERY_MIN > 0 and BACKFILL_HOURS == 0 and
            (not last_backup or (now - last_backup).total_seconds() >= BACKUP_EVERY_MIN * 60 - 90)):
        try:
            feed_ids = {str(r.get("id")) for r in rows}
            awake = parse_time(state.get("awake_since")) if state.get("awake_since") else None
            backup_age = BACKUP_MAX_AGE_MIN
            if awake:
                backup_age = max(15, min(BACKUP_MAX_AGE_MIN, int((now - awake).total_seconds() // 60) + 10))
            rows += fetch_backup(client, seen_set | feed_ids, backup_age)
            state["backup_last"] = now.isoformat()
        except Exception as ex:
            print("Backup error (main feed par asar nahi):", ex)

    stats = {"new": 0, "fail": 0, "skip": 0, "apply": 0, "backup": 0}
    jobs = sorted((normalize(r) for r in rows), key=lambda j: j["posted"] or "")
    for job in jobs:
        uid = job["uid"]
        if not uid or uid in seen_set:
            continue
        seen.append(uid)
        seen_set.add(uid)

        ago = minutes_ago(job["posted"])
        max_age = BACKUP_MAX_AGE_MIN if job["source"] == "backup" else FRESH_MAX_AGE_MIN
        if BACKFILL_HOURS == 0 and ago is not None and ago > max_age:
            print(f"Purani job, chhod di ({ago} min): {job['title'][:50]}")
            continue

        stats["new"] += 1
        if job["source"] == "backup":
            stats["backup"] += 1
        ok, info, niche = check(job)
        if not ok:
            stats["fail"] += 1
            print(f"Sharten fail: {job['title'][:50]} ({info})")
            if SEND_SKIPPED and niche:            # kachra (niche se bahar) Slack pe nahi
                try:
                    notify_rejected(job, info)
                except Exception as ex:
                    print("Slack error:", ex)
            continue
        info["code_score"], info["code_why"] = code_score(job)
        info["tag"] = angle_tag(job)
        try:
            ai = ask_ai(job_to_text(job, info))
        except Exception as ex:
            print("AI error:", ex)
            slack_job([f"⚠️ AI error, khud dekh lein: *{esc(job['title'])}*",
                       f"💰 {esc(money_text(job))}  |  🧮 Code {info['code_score']}/10"], job["url"])
            continue
        try:
            if ai.get("apply"):
                stats["apply"] += 1
                notify(job, info, ai)
            else:
                stats["skip"] += 1
                print(f"AI skip: {job['title'][:50]} ({ai.get('reason')})")
                if SEND_SKIPPED:
                    notify_skipped(job, info, ai)
        except Exception as ex:
            print("Slack error:", ex)
        kv.set_record("STATE", {**state, "seen": seen[-3000:], "hb_started": True, "hb_feed": FEED_ID})

    state = {**state, "seen": seen[-3000:], "hb_started": True, "hb_feed": FEED_ID}
    try:
        state = send_status(state, stats)
    except Exception as ex:
        print("Status message error:", ex)
    kv.set_record("STATE", state)
    print(f"===== HISAAB: {stats['new']} nayi | {stats['fail']} sharton pe kati | "
          f"{stats['skip']} AI ne skip ki | {stats['apply']} apply (Slack) | {stats['backup']} backup se =====")


def send_status(state, stats):
    """Run ka chhota sa hisaab Slack pe, taake pata rahe system chal raha hai."""
    if STATUS_MESSAGES == "off" or BACKFILL_HOURS > 0:
        return state
    total = {"new": 0, "fail": 0, "skip": 0, "apply": 0, "backup": 0}
    total.update(state.get("status_totals") or {})
    for k in total:
        total[k] += stats.get(k, 0)
    now = datetime.now(timezone.utc)
    last = parse_time(state.get("status_last")) if state.get("status_last") else None
    if STATUS_MESSAGES == "hourly" and last and (now - last).total_seconds() < 3600:
        return {**state, "status_totals": total}
    local = now.astimezone(PKT)
    stamp = f"{local.day} {local.strftime('%b')}, {local.strftime('%I:%M %p').lstrip('0')}"
    if total["new"] == 0:
        msg = f"🔍 {stamp}: No New Jobs 🙁"
    else:
        msg = (f"🔍 {stamp}: {total['new']} New Jobs  |  {total['fail']} Fail  |  "
               f"{total['skip']} Skip  |  {total['apply']} Apply")
        if total["backup"]:
            msg += f"  |  🛟 {total['backup']} Backup"
    slack_text(msg)
    return {**state, "status_totals": {"new": 0, "fail": 0, "skip": 0, "apply": 0, "backup": 0},
            "status_last": now.isoformat()}


if __name__ == "__main__":
    main()
