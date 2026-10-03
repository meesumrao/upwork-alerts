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
FEED_ID = "meesum-shopify"            # Hyperbach isi naam se yaad rakhta hai kaunsi jobs bhej chuka
STORE_NAME = "upwork-shopify-state"
KEYWORDS = ["shopify"]                # ye lafz job ke TITLE ya SKILLS mein hona chahiye
MY_COUNTRY = "Pakistan"
LIMIT = 200                           # ek run mein max jobs (safety)
FRESH_MAX_AGE_MIN = 120               # is se purani job normal run mein notify nahi hogi
BACKFILL_HOURS = int(os.getenv("BACKFILL_HOURS") or "0")
BACKFILL_LIMIT = 300
SEND_SKIPPED = (os.getenv("SEND_SKIPPED") or "false").lower() == "true"

MIN_FIXED = 250
MIN_HIRE_RATE = 40        # is se upar
MIN_SPENT_PER_HIRE = 50   # is se upar

HERE = Path(__file__).parent
CRITERIA = (HERE / "prompt.txt").read_text(encoding="utf-8")
PROPOSAL_FORMAT = (HERE / "proposal_format.txt").read_text(encoding="utf-8")


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
    }


# ---------- Sharten ----------
def keyword_match(job):
    title = job["title"].lower()
    skills = " ".join(job["skills"]).lower()
    return any(k.lower() in title or k.lower() in skills for k in KEYWORDS)


def check(job):
    if not keyword_match(job):
        return False, "keyword title/skills mein nahi"
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
def job_to_text(job, info):
    lines = [
        f"Title: {job['title']}",
        f"Budget: {money_text(job)}",
        f"Client: {job['country']}, spent ${job['spent'] or 0:,.0f}, {job['hires']:.0f} hires, "
        f"hire rate {job['hire_rate']:.0f}%, rating {job['rating'] or '?'}",
        f"Skills: {', '.join(job['skills'])}",
        f"Experience level: {job['experience']}",
    ]
    if job["anti_bot"]:
        lines.append(f"IMPORTANT - client asks to include this exact word/phrase at the start of the proposal: {job['anti_bot']}")
    if job["must_include"]:
        lines.append(f"Client's requirements for the proposal: {job['must_include']}")
    lines += ["", "Description:", job["description"]]
    if job["questions"]:
        lines += ["", "Screening questions:"] + [f"{i}. {q}" for i, q in enumerate(job["questions"], 1)]
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You evaluate Upwork jobs for a freelancer and write proposals.

## Freelancer's criteria for deciding whether to apply:
{CRITERIA}

## Proposal format (follow it exactly):
{PROPOSAL_FORMAT}

Respond ONLY with a JSON object, no other text:
{{"apply": true or false,
  "score": integer 0-10 (how good a fit),
  "reason": "very short reason, max 12 words",
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


REVIEW_PROMPT = f"""You are a strict Upwork proposal editor. You check a draft proposal against every rule below and fix it.

## Rules the proposal must follow:
{PROPOSAL_FORMAT}

Score the draft from 0 to 10 against EVERY rule (free element in the hook, every point from the job covered, shortness, human style, no dashes or hyphens, no contractions, strong reason to reply, portfolio list order, anti-bot word if asked).
If it is not a 10/10, rewrite it until it is 10/10. Keep what is already good.
Also check the screening answers with the same human style rules.

Respond ONLY with a JSON object, no other text:
{{"score": integer 0-10 for the FINAL version,
  "proposal": "the final 10/10 proposal",
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


def job_lines(job, info, ai, label):
    lines = [
        f"{label} *{esc(job['title'])}*",
        f"💰 {esc(money_text(job))}  |  ⭐ {ai.get('score', '?')}/10  |  🏢 {esc(job['country'])}",
        f"${job['spent'] or 0:,.0f} spent  |  {job['hires']:.0f} hires (${info['per_hire']:,.0f}/hire)  |  "
        f"{job['hire_rate']:.0f}% hire rate",
    ]
    extra = [posted_text(job)]
    if job["applicants"] is not None:
        extra.append(f"👥 {job['applicants']} proposals")
    lines.append("  |  ".join(e for e in extra if e))
    lines.append(f"🧠 {esc(ai.get('reason', ''))}")
    return lines


def notify(job, info, ai):
    slack_job(job_lines(job, info, ai, "✅ Apply:"), job["url"])
    # Proposal aur screening Q&A ek hi message mein (long-press -> Copy text)
    text = humanize(ai.get("proposal", ""))
    answers = ai.get("screening_answers") or []
    if answers:
        qs = job["questions"]
        qa = []
        for i, ans in enumerate(answers):
            q = qs[i] if i < len(qs) else f"Question {i + 1}"
            qa.append(f"Q: {q}\nA: {humanize(str(ans))}")
        text += "\n\n\n" + "\n\n".join(qa)
    slack_text(text)


def notify_skipped(job, info, ai):
    slack_job(job_lines(job, info, ai, "⏭ Skipped:"), job["url"])


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

    run = client.actor(ACTOR_ID).call(run_input=run_input, timeout_secs=240)
    if not run_ok(run):
        raise RuntimeError(f"Actor run fail: {field(run, 'status')}")
    dataset_id = field(run, "defaultDatasetId", "default_dataset_id")
    items = field(client.dataset(dataset_id).list_items(), "items") or []
    return [i if isinstance(i, dict) else (i.model_dump() if hasattr(i, "model_dump") else dict(i))
            for i in items]


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
    first_feed_run = not state.get("hb_started")
    if len(rows) >= limit and not (first_feed_run and BACKFILL_HOURS == 0):
        slack_text(f"⚠️ Limit ({limit}) poori ho gayi, kuch jobs miss ho sakti hain.")

    jobs = sorted((normalize(r) for r in rows), key=lambda j: j["posted"] or "")
    for job in jobs:
        uid = job["uid"]
        if not uid or uid in seen_set:
            continue
        seen.append(uid)
        seen_set.add(uid)

        ago = minutes_ago(job["posted"])
        if BACKFILL_HOURS == 0 and ago is not None and ago > FRESH_MAX_AGE_MIN:
            print(f"Purani job, chhod di ({ago} min): {job['title'][:50]}")
            continue

        ok, info = check(job)
        if not ok:
            print(f"Sharten fail: {job['title'][:50]} ({info})")
            continue
        try:
            text = job_to_text(job, info)
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
                notify(job, info, ai)
            else:
                print(f"AI skip: {job['title'][:50]} ({ai.get('reason')})")
                if SEND_SKIPPED:
                    notify_skipped(job, info, ai)
        except Exception as ex:
            print("Slack error:", ex)
        kv.set_record("STATE", {"seen": seen[-3000:], "hb_started": True})

    kv.set_record("STATE", {"seen": seen[-3000:], "hb_started": True})
    print("Done")


if __name__ == "__main__":
    main()
