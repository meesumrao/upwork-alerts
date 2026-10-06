"""
Upwork jobs -> AI (apply ya nahi + proposal) -> Slack
Data source: Apify actor hyperbach/upwork-scraper-ai (jobs ~1-4 minute mein)

Sharten:
  - Hourly: koi bhi rate
  - Fixed: minimum $250
  - Payment verified
  - Client hire rate > 40%
  - Client total spent / total hires > $50
  - Client ne Pakistan ke freelancers ko allow kiya ho
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from apify_client import ApifyClient

# ---------- Secrets ----------
APIFY_TOKEN = os.environ["APIFY_TOKEN"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
AI_API_KEY = os.environ["AI_API_KEY"]
AI_BASE_URL = os.getenv("AI_BASE_URL") or "https://api.openai.com/v1"
AI_MODEL = os.getenv("AI_MODEL") or "gpt-5.4"

# ---------- Settings ----------
ACTOR_ID = "hyperbach/upwork-scraper-ai"
FEED_ID = "meesum-shopify-v2"         # Hyperbach isi naam se yaad rakhta hai kaunsi jobs bhej chuka
STORE_NAME = "upwork-shopify-state"
# Hyperbach se wahi jobs mangwao jin mein kahin bhi ye lafz hon (baaqi chhaant script karti hai)
KEYWORDS = ["shopify", "shopifyplus", "ecommerce", "e-commerce", "e commerce"]

# Rule 1: title ya skills mein shopify / ecommerce ho
CORE_RE = re.compile(r"shopify|\be[\s\-\u2010\u2011]?commerce", re.I)
# Rule 2: title mein in mein se koi kaam ho, aur job mein kahin bhi shopify / ecommerce ho
ROLE_RE = re.compile(
    r"\b(meta ads?|fb ads?|facebook ads?|tik ?tok ads?|instagram ads?|"
    r"virtual assistants?|va|cust(?:omer)? services?|cust(?:omer)? support|"
    r"managers?|management|operators?|operations?|list\w*|cro)\b", re.I)
MY_COUNTRY = "Pakistan"
LIMIT = 200                           # ek run mein max jobs (safety)
FRESH_MAX_AGE_MIN = 120               # is se purani job normal run mein notify nahi hogi

# Backup: Hyperbach se chhooti jobs pakadne ke liye Black Falcon (seedha Upwork live search)
BACKUP_ACTOR = "blackfalcondata/upwork-scraper"
BACKUP_EVERY_MIN = int(os.getenv("BACKUP_EVERY_MIN") or "15")   # 0 = backup band
BACKUP_MAX_AGE_MIN = 240              # backup sirf itni purani jobs dekhta hai
BACKUP_QUERY = "shopify OR shopifyplus OR ecommerce OR \"e-commerce\" OR \"e commerce\""
BACKFILL_HOURS = int(os.getenv("BACKFILL_HOURS") or "0")
BACKFILL_LIMIT = 300
SEND_SKIPPED = (os.getenv("SEND_SKIPPED") or "false").lower() == "true"
# Har run ke baad status message: "every_run" (har 5 min), "hourly" (ghante mein ek), "off"
STATUS_MESSAGES = (os.getenv("STATUS_MESSAGES") or "off").lower()

MIN_FIXED = 250
MIN_HIRE_RATE = 40        # is se upar
MIN_SPENT_PER_HIRE = 50   # is se upar

HERE = Path(__file__).parent
CRITERIA = (HERE / "prompt.txt").read_text(encoding="utf-8")
PROPOSAL_FORMAT = (HERE / "proposal_format.txt").read_text(encoding="utf-8")
EXP_FILE = HERE / "experience.txt"
EXPERIENCE = EXP_FILE.read_text(encoding="utf-8") if EXP_FILE.exists() else ""


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
        "country": row.get("client_location") or "?",
        "payment_verified": bool(row.get("buyer_payment_verified")),
        "hire_rate": num(row.get("hire_rate")),
        "hires": num(row.get("hires")),
        "spent": num(row.get("total_spent")),
        "rating": row.get("buyer_score"),
        "applicants": row.get("client_total_applicants"),
        "questions": qs,
        "anti_bot": row.get("ai_anti_bot_phrase"),
        "must_include": row.get("ai_specific_requirements_before_applying"),
        "allowed_countries": row.get("qual_countries"),
        "posted": row.get("date_posted"),
        "source": row.get("_source") or "feed",
    }


# ---------- Sharten ----------
def keyword_match(job):
    title = job["title"]
    skills = " ".join(job["skills"])
    if CORE_RE.search(title) or CORE_RE.search(skills):
        return True
    if ROLE_RE.search(title):
        everything = " ".join([title, skills, job["description"]])
        return bool(CORE_RE.search(everything))
    return False


def check(job):
    if not keyword_match(job):
        return False, "keyword rule match nahi"
    if not job["payment_verified"]:
        return False, "payment not verified"
    if (job["hire_rate"] or 0) <= MIN_HIRE_RATE:
        return False, f"hire rate {job['hire_rate']}%"
    hires, spent = job["hires"] or 0, job["spent"] or 0
    if hires <= 0:
        return False, "no hires"
    per_hire = spent / hires
    if per_hire <= MIN_SPENT_PER_HIRE:
        return False, f"${per_hire:.0f}/hire"
    if job["kind"] == "fixed" and (job["fixed_budget"] or 0) < MIN_FIXED:
        return False, "fixed budget low"
    allowed = job["allowed_countries"]
    if allowed and MY_COUNTRY not in allowed:
        return False, f"{MY_COUNTRY} allowed nahi"
    return True, {"per_hire": per_hire}


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
def job_to_text(job, info, est=None):
    lines = [
        f"Title: {job['title']}",
        f"Budget: {money_text(job)}",
        f"Client: {job['country']}, spent ${job['spent'] or 0:,.0f}, {job['hires']:.0f} hires, "
        f"hire rate {job['hire_rate']:.0f}%, rating {job['rating'] or '?'}",
        f"Skills: {', '.join(job['skills'])}",
        f"Experience level: {job['experience']}",
    ]
    keywords = [job["title"]] + job["skills"]
    lines.append("Job title and skill words (use naturally where they fit, skipping some is fine, never as a comma list): "
                 + " | ".join(k for k in keywords if k))
    if job["anti_bot"]:
        lines.append(f"IMPORTANT - client asks to include this exact word/phrase at the start of the proposal: {job['anti_bot']}")
    if job["must_include"]:
        lines.append(f"Client's requirements for the proposal: {job['must_include']}")
    if est:
        lines.append(f"MY PRICE ESTIMATE (NEVER mention it unless the client asked for a price, rate or quote, and even then "
                     f"only if the scope is fully defined, otherwise say it depends on the final scope): "
                     f"fixed {est.get('fixed', '?')} for about {est.get('hours', '?')} hours, or hourly {est.get('hourly', '?')}")
    lines += ["", "Description:", job["description"]]
    if job["questions"]:
        lines += ["", "Screening questions:"] + [f"{i}. {q}" for i, q in enumerate(job["questions"], 1)]
    return "\n".join(lines)


ESTIMATE_PROMPT = """You price Upwork jobs for Meesum, a Shopify and ecommerce expert with 12+ years of experience.
You are NOT shown the client's budget on purpose. Judge only from the work itself.
Estimate how many hours an experienced freelancer would really need, and a fair, competitive Upwork price for
this work: a fixed price for the whole job and an hourly rate. Be realistic for the Upwork market, not too high, not too low.
For ongoing roles (VA, support, ads management, store manager) give the hourly rate and a fixed price per month at the
likely weekly hours.

Respond ONLY with a JSON object, no other text:
{"hours": "e.g. 8-12 (or e.g. 20/week for ongoing)",
 "fixed": "e.g. $300-400 (or e.g. $1,200/month for ongoing)",
 "hourly": "e.g. $25-35/hr",
 "best": "fixed or hourly",
 "why": "max 10 words"}"""


def estimate_rate(job):
    """Budget dikhaye baghair, sirf kaam dekh ke rate ka andaza."""
    lines = [f"Title: {job['title']}", f"Skills: {', '.join(job['skills'])}",
             f"Experience level: {job['experience']}", f"Job type the client chose: {job['kind']}",
             "", "Description:", job["description"]]
    if job["questions"]:
        lines += ["", "Screening questions:"] + [f"- {q}" for q in job["questions"]]
    r = requests.post(
        f"{AI_BASE_URL.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {AI_API_KEY}"},
        json={"model": AI_MODEL,
              "messages": [{"role": "system", "content": ESTIMATE_PROMPT},
                           {"role": "user", "content": "\n".join(lines)}],
              "response_format": {"type": "json_object"}},
        timeout=90,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"].strip()
    content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(content)


SYSTEM_PROMPT = f"""You evaluate Upwork jobs for a freelancer and write proposals.

## Freelancer's criteria for deciding whether to apply:
{CRITERIA}

## Meesum's real past work (pick the 1 or 2 projects most relevant to THIS job, never invent anything):
{EXPERIENCE}

## Proposal format (follow it exactly):
{PROPOSAL_FORMAT}

Respond ONLY with a JSON object, no other text:
{{"apply": true or false,
  "score": integer 0-10 (how good a fit),
  "summary": "2 to 3 short plain sentences: what the client needs, the main tasks or deliverables, and any timeline, tool or special requirement",
  "reason": "very short reason for the decision, max 12 words",
  "proposal": "full proposal text if apply is true, otherwise empty string",
  "screening_answers": ["one answer per screening question, empty list if none"]}}"""


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


REVIEW_PROMPT = f"""You are a light editor for an Upwork proposal. The writer's voice is good, keep it.
Do NOT restructure it, do NOT make it more formal, do NOT add paragraphs or new sentences.

The rules the proposal follows:
{PROPOSAL_FORMAT}

Only do these things:
1. Fix any hard rule that is broken: anti-bot word missing from the first line, hook not a single ALL CAPS line with a small free offer and " 😎",
   a price or rate that the client did not ask for (remove it), contractions, dashes, hyphens, arrows, bullet points,
   banned phrases, a comma list of skills that was not asked for, or the website list missing or out of order.
2. Make sure every question and request in the job got an answer. If one is missing, add it in a few words.
   Keep any mention of a past project, do not add new projects or numbers.
3. Make it SHORTER: cut filler, repeated points and anything the client would skip. Never make it longer except to answer something missing.
4. Screening answers: one plain sentence each, under 20 words.

Respond ONLY with a JSON object, no other text:
{{"score": integer 0-10 for the final version,
  "proposal": "the final proposal",
  "screening_answers": ["final answer per screening question, empty list if none"]}}"""


def review_ai(job_text, ai):
    draft = {"proposal": ai.get("proposal", ""), "screening_answers": ai.get("screening_answers") or []}
    r = requests.post(
        f"{AI_BASE_URL.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {AI_API_KEY}"},
        json={"model": AI_MODEL,
              "messages": [{"role": "system", "content": REVIEW_PROMPT},
                           {"role": "user", "content": f"JOB:\n{job_text}\n\nDRAFT:\n{json.dumps(draft, ensure_ascii=False)}"}],
              "response_format": {"type": "json_object"}},
        timeout=120,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"].strip()
    content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    fixed = json.loads(content)
    if fixed.get("proposal"):
        ai["proposal"] = fixed["proposal"]
        ai["screening_answers"] = fixed.get("screening_answers") or ai.get("screening_answers") or []
    return ai


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


EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+")


def strip_emoji(text):
    """Slack mobile copy mein emoji ':sunglasses:' ban jata hai, is liye proposal se hata do."""
    cleaned = EMOJI_RE.sub("", text or "")
    return "\n".join(re.sub(r" {2,}", " ", line).rstrip() for line in cleaned.split("\n"))


CONTRACTIONS = {"I'm": "I am", "I've": "I have", "I'll": "I will", "I'd": "I would"}
DOMAIN_LINE = re.compile(r"^\s*\S+\.\S+\s*$")


WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")
SENT_END_RE = re.compile(r"(?<!\.)[.?!][\"')]*\s+$")


def sentence_case(line):
    """Capital sirf jumle ke shuru mein (aur 'I' aur CRO/SEO jaise chhote acronyms), baaqi sab chhota."""
    def fix(m):
        word = m.group(0)
        before = line[:m.start()]
        if word == "I" or word.startswith("I'"):
            return word
        if word.isupper() and 2 <= len(word) <= 6:
            return word                                   # CRO, SEO, COD, AWB, API
        if before.strip() == "" or SENT_END_RE.search(before):
            return word[0].upper() + word[1:].lower()      # jumle ka pehla lafz
        return word.lower()
    return WORD_RE.sub(fix, line)


def format_proposal(text, anti_bot=None):
    """Hook poora CAPS mein, body mein capital sirf jumlon ke shuru mein."""
    lines = text.split("\n")
    hi_idx = next((i for i, l in enumerate(lines) if re.match(r"\s*hi\b", l, re.I)), None)
    name_idx = next((i for i, l in enumerate(lines) if l.strip().lower() == "meesum"), len(lines))
    out = []
    for i, line in enumerate(lines):
        if hi_idx is not None and i < hi_idx:
            if anti_bot and line.strip().lower() == str(anti_bot).strip().lower():
                out.append(line)                           # client ka maanga hua lafz waisa hi
            else:
                out.append(line.upper())                   # poora hook CAPS
        elif hi_idx is not None and hi_idx < i < name_idx:
            out.append(sentence_case(line))
        else:
            out.append(line)
    return "\n".join(out)


def humanize(text):
    """Proposal copy-ready: emoji nahi, dash/hyphen nahi, I'm/I've waghera poore."""
    text = strip_emoji(text).replace("\u2019", "'")
    for short, full in CONTRACTIONS.items():
        text = re.sub(rf"\b{re.escape(short)}\b", full, text)
        text = re.sub(rf"\b{re.escape(short.upper())}\b", full.upper(), text)
    out = []
    for line in text.split("\n"):
        if not DOMAIN_LINE.match(line):                       # website list ko na chhedo
            line = re.sub(r"\s*[\u2014\u2013]\s*", ", ", line)  # em/en dash
            line = re.sub(r"\s+-\s+", ", ", line)                # " - "
            line = re.sub(r"(?<=\w)-(?=\w)", " ", line)            # day-to-day -> day to day
            line = re.sub(r" {2,}", " ", line).rstrip()
        out.append(line)
    return "\n".join(out)


PKT = timezone(timedelta(hours=5))


def posted_text(job):
    t = parse_time(job["posted"])
    if not t:
        return None
    ago = minutes_ago(job["posted"])
    local = t.astimezone(PKT)
    stamp = f"{local.day} {local.strftime('%b')}, {local.strftime('%I:%M %p').lstrip('0')}"
    return f"⏱ Posted {ago} min ago ({stamp})"


def job_lines(job, info, ai, label, est=None):
    lines = [
        f"{label} *{esc(job['title'])}*",
        f"💰 {esc(money_text(job))}  |  ⭐ {ai.get('score', '?')}/10  |  🏢 {esc(job['country'])}",
        f"${job['spent'] or 0:,.0f} spent  |  {job['hires']:.0f} hires (${info['per_hire']:,.0f}/hire)  |  "
        f"{job['hire_rate']:.0f}% hire rate",
    ]
    extra = [posted_text(job)]
    if job.get("source") == "backup":
        extra.append("🛟 Backup")
    if job["applicants"] is not None:
        extra.append(f"👥 {job['applicants']} proposals")
    lines.append("  |  ".join(e for e in extra if e))
    if ai.get("summary"):
        lines.append(f"📝 {esc(ai['summary'])}")
    if est:
        best = str(est.get("best", "")).lower()
        lines.append(f"💵 AI rate: Fixed {esc(est.get('fixed', '?'))} (~{esc(est.get('hours', '?'))} hrs)  |  "
                     f"Hourly {esc(est.get('hourly', '?'))}" + (f"  |  Best: {esc(best)}" if best else ""))
    lines.append(f"🧠 {esc(ai.get('reason', ''))}")
    return lines


def notify(job, info, ai, est=None):
    slack_job(job_lines(job, info, ai, "✅ Apply:", est), job["url"])
    # Proposal aur screening Q&A ek hi message mein (long-press -> Copy text)
    text = format_proposal(humanize(ai.get("proposal", "")), job.get("anti_bot"))
    answers = ai.get("screening_answers") or []
    if answers:
        qs = job["questions"]
        qa = []
        for i, ans in enumerate(answers):
            q = qs[i] if i < len(qs) else f"Question {i + 1}"
            qa.append(f"Q: {q}\nA: {sentence_case(humanize(str(ans)))}")
        text += "\n\n\n" + "\n\n".join(qa)
    slack_text(text)


def notify_skipped(job, info, ai, est=None):
    slack_job(job_lines(job, info, ai, "⏭ Skipped:", est), job["url"])


# ---------- Main ----------
def fetch_jobs(client):
    run_input = {
        "any_words": KEYWORDS,
        # Server pe sharten (reject hui jobs ke paise nahi lagte)
        "buyer_payment_verified": True,
        "hire_rate": f">={MIN_HIRE_RATE + 1}",
        "hires": ">=1",
        "total_spent": f">={MIN_SPENT_PER_HIRE}",
        "clientId": FEED_ID,
        "whats_new": False,
    }
    if BACKFILL_HOURS > 0:
        print(f"BACKFILL: pichle {BACKFILL_HOURS} ghante ki jobs")
        run_input.update({"notifications_only": False,
                          "date_posted": f"{BACKFILL_HOURS}h",
                          "limit": BACKFILL_LIMIT})
    else:
        # Har run sirf pichle run ke baad aayi jobs deta hai, har job ka ek hi dafa bill
        run_input.update({"notifications_only": True, "limit": LIMIT})

    return run_actor(client, ACTOR_ID, run_input)


def run_actor(client, actor_id, run_input, timeout=240):
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
    if row.get("clientPaymentVerified") is False:
        return False
    spent = num(row.get("clientTotalSpent"))
    if spent is not None and spent < MIN_SPENT_PER_HIRE:
        return False
    if "FIXED" in str(row.get("jobType") or "").upper():
        budget = num(row.get("budgetAmount"))
        if budget is not None and budget < MIN_FIXED:
            return False
    return True


def fetch_backup(client, known_ids):
    """Black Falcon se nayi jobs, phir jo chhooti hon un ka poora data Hyperbach se (link ke zariye)."""
    bf_rows = run_actor(client, BACKUP_ACTOR, {
        "query": BACKUP_QUERY,
        "sort": "recency",
        "verifiedPaymentOnly": True,
        "minClientTotalSpent": MIN_SPENT_PER_HIRE,
        "maxAgeMinutes": BACKUP_MAX_AGE_MIN,
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


def main():
    client = ApifyClient(APIFY_TOKEN)
    store = client.key_value_stores().get_or_create(name=STORE_NAME)
    kv = client.key_value_store(field(store, "id"))
    state = field(kv.get_record("STATE"), "value") or {}
    seen = state.get("seen", [])
    seen_set = set(seen)

    rows = fetch_jobs(client)
    print(f"Hyperbach ne {len(rows)} jobs di")
    limit = BACKFILL_LIMIT if BACKFILL_HOURS > 0 else LIMIT
    first_feed_run = state.get("hb_feed") != FEED_ID
    if len(rows) >= limit and not (first_feed_run and BACKFILL_HOURS == 0):
        slack_text(f"⚠️ Limit ({limit}) poori ho gayi, kuch jobs miss ho sakti hain.")

    # Backup har BACKUP_EVERY_MIN minute (main feed se chhooti jobs)
    now = datetime.now(timezone.utc)
    last_backup = parse_time(state.get("backup_last")) if state.get("backup_last") else None
    if (BACKUP_EVERY_MIN > 0 and BACKFILL_HOURS == 0 and
            (not last_backup or (now - last_backup).total_seconds() >= BACKUP_EVERY_MIN * 60 - 90)):
        try:
            feed_ids = {str(r.get("id")) for r in rows}
            rows += fetch_backup(client, seen_set | feed_ids)
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
        ok, info = check(job)
        if not ok:
            stats["fail"] += 1
            print(f"Sharten fail: {job['title'][:50]} ({info})")
            continue
        est = None
        try:
            est = estimate_rate(job)
        except Exception as ex:
            print("Rate estimate error:", ex)
        try:
            text = job_to_text(job, info, est)
            ai = ask_ai(text)
            if ai.get("apply"):
                try:
                    ai = review_ai(text, ai)
                except Exception as ex:
                    print("Review error (pehla draft bheja):", ex)
        except Exception as ex:
            print("AI error:", ex)
            slack_job([f"⚠️ AI error, khud dekh lein: *{esc(job['title'])}*",
                       f"💰 {esc(money_text(job))}"], job["url"])
            continue
        try:
            if ai.get("apply"):
                stats["apply"] += 1
                notify(job, info, ai, est)
            else:
                stats["skip"] += 1
                print(f"AI skip: {job['title'][:50]} ({ai.get('reason')})")
                if SEND_SKIPPED:
                    notify_skipped(job, info, ai, est)
        except Exception as ex:
            print("Slack error:", ex)
        kv.set_record("STATE", {**state, "seen": seen[-3000:], "hb_started": True, "hb_feed": FEED_ID})

    state = {**state, "seen": seen[-3000:], "hb_started": True, "hb_feed": FEED_ID}
    try:
        state = send_status(state, stats)
    except Exception as ex:
        print("Status message error:", ex)
    kv.set_record("STATE", state)
    print(f"Done: {stats}")


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
