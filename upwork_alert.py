"""
Upwork Shopify jobs -> AI (apply ya nahi + proposal) -> Slack

Flow:
  1. Apify actor (upwork-vibe) se sirf nayi jobs
  2. Aapki sharten: hourly koi bhi, fixed >= $250, payment verified,
     hire rate > 40%, spent/hires > $50
  3. AI aapke prompt.txt ke hisaab se faisla karta hai, aur apply karna ho
     to proposal_format.txt ke format mein proposal likhta hai
  4. Slack: job details + "Open Job" button, phir proposal alag message mein
     (long-press -> Copy text = poora proposal copy)
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from apify_client import ApifyClient

# ---------- Secrets ----------
APIFY_TOKEN = os.environ["APIFY_TOKEN"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
AI_API_KEY = os.environ["AI_API_KEY"]
# OpenAI default. Gemini (free tier) ke liye:
#   AI_BASE_URL = https://generativelanguage.googleapis.com/v1beta/openai
AI_BASE_URL = os.getenv("AI_BASE_URL") or "https://api.openai.com/v1"
AI_MODEL = os.getenv("AI_MODEL") or "gpt-5.4"

# ---------- Settings ----------
ACTOR_ID = "upwork-vibe/upwork-job-scraper"
STORE_NAME = "upwork-shopify-state"
KEYWORDS = ["Shopify"]
LAG_MINUTES = int(os.getenv("LAG_MINUTES") or "30")
FIRST_RUN_LOOKBACK_MIN = 60
MAX_WINDOW_HOURS = 6
LIMIT = 100
SEND_SKIPPED = (os.getenv("SEND_SKIPPED") or "false").lower() == "true"

MIN_FIXED = 250
MIN_HIRE_RATE = 40        # is se upar
MIN_SPENT_PER_HIRE = 50   # is se upar

HERE = Path(__file__).parent
CRITERIA = (HERE / "prompt.txt").read_text(encoding="utf-8")
PROPOSAL_FORMAT = (HERE / "proposal_format.txt").read_text(encoding="utf-8")


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
    return "SUCCEEDED" in str(field(run, "status", "status_") or "")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- Sharten ----------
def job_kind(job):
    jt = str(job.get("jobType") or job.get("type") or "").upper()
    if "HOURLY" in jt:
        return "hourly"
    if "FIXED" in jt:
        return "fixed"
    return "fixed" if ((job.get("budget") or {}).get("fixedBudget") or 0) > 0 else "hourly"


def check(job):
    client = job.get("client") or {}
    stats = client.get("stats") or {}
    budget = job.get("budget") or {}
    if not client.get("paymentMethodVerified"):
        return False, "payment not verified"
    hire_rate = stats.get("hireRate") or 0
    if hire_rate <= MIN_HIRE_RATE:
        return False, f"hire rate {hire_rate}%"
    hires = stats.get("totalHires") or 0
    spent = stats.get("totalSpent") or 0
    if hires <= 0:
        return False, "no hires"
    per_hire = spent / hires
    if per_hire <= MIN_SPENT_PER_HIRE:
        return False, f"${per_hire:.0f}/hire"
    kind = job_kind(job)
    if kind == "fixed" and (budget.get("fixedBudget") or 0) < MIN_FIXED:
        return False, "fixed budget low"
    return True, {"kind": kind, "hire_rate": hire_rate, "hires": hires,
                  "spent": spent, "per_hire": per_hire}


def money_text(job, kind):
    budget = job.get("budget") or {}
    if kind == "fixed":
        return f"Fixed ${budget.get('fixedBudget', 0):,.0f}"
    rate = budget.get("hourlyRate") or {}
    lo, hi = rate.get("min"), rate.get("max")
    return f"Hourly ${lo}-${hi}/hr" if (lo or hi) else "Hourly (rate not given)"


def questions_list(job):
    out = []
    for q in job.get("questions") or []:
        out.append(q if isinstance(q, str) else (q.get("question") or q.get("text") or str(q)))
    return out


# ---------- AI ----------
def job_to_text(job, info):
    client = job.get("client") or {}
    lines = [
        f"Title: {job.get('title', '')}",
        f"Budget: {money_text(job, info['kind'])}",
        f"Client: {client.get('countryCode', '?')}, spent ${info['spent']:,.0f}, "
        f"{info['hires']} hires, hire rate {info['hire_rate']}%, "
        f"rating {(client.get('stats') or {}).get('feedbackRate', '?')}",
        f"Skills: {', '.join(job.get('skills') or [])}",
        f"Experience level: {(job.get('vendor') or {}).get('experienceLevel', '?')}",
        "",
        "Description:",
        job.get("description", ""),
    ]
    qs = questions_list(job)
    if qs:
        lines += ["", "Screening questions:"] + [f"{i}. {q}" for i, q in enumerate(qs, 1)]
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You evaluate Upwork jobs for a freelancer and write proposals.

## Freelancer's criteria for deciding whether to apply:
{CRITERIA}

## Proposal format (follow it exactly):
{PROPOSAL_FORMAT}

Respond ONLY with a JSON object, no other text:
{{"apply": true or false,
  "score": integer 0-10 (how good a fit),
  "reason": "1-2 sentence reason",
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
    """Saada message (proposal waghera) - mrkdwn off taake text bilkul waisa hi copy ho."""
    slack_post({"text": text[:39000], "mrkdwn": False})


def slack_job(lines, url=None):
    """Job details + Open Job button."""
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


def notify(job, info, ai):
    url = job.get("externalLink") or f"https://www.upwork.com/jobs/{job.get('ciphertext', '')}"
    lines = [
        f"✅ *{esc(job.get('title', 'Untitled'))}*",
        f"💰 {esc(money_text(job, info['kind']))}  |  ⭐ Fit: {ai.get('score', '?')}/10",
        f"🏢 {esc((job.get('client') or {}).get('countryCode', '?'))}  |  "
        f"${info['spent']:,.0f} spent  |  {info['hires']} hires (${info['per_hire']:,.0f}/hire)  |  "
        f"{info['hire_rate']}% hire rate",
        f"🧠 {esc(ai.get('reason', ''))}",
    ]
    if job.get("applicationCost"):
        lines.append(f"🎟 Connects: {job['applicationCost']}")
    slack_job(lines, url)

    # Proposal akela message: long-press -> Copy text
    slack_text(ai.get("proposal", ""))

    qs = questions_list(job)
    for i, ans in enumerate(ai.get("screening_answers") or []):
        q = qs[i] if i < len(qs) else f"Question {i + 1}"
        slack_text(f"Q: {q}\n\n{ans}")


# ---------- Main ----------
def main():
    client = ApifyClient(APIFY_TOKEN)
    store = client.key_value_stores().get_or_create(name=STORE_NAME)
    kv = client.key_value_store(field(store, "id"))
    record = kv.get_record("STATE")
    state = field(record, "value") or {}
    seen = state.get("seen", [])

    now = datetime.now(timezone.utc)
    to_dt = now - timedelta(minutes=LAG_MINUTES)
    if state.get("cursor"):
        from_dt = datetime.fromisoformat(state["cursor"].replace("Z", "+00:00"))
    else:
        from_dt = to_dt - timedelta(minutes=FIRST_RUN_LOOKBACK_MIN)
    from_dt = max(from_dt, to_dt - timedelta(hours=MAX_WINDOW_HOURS))
    if from_dt >= to_dt:
        print("Window khaali hai.")
        return

    base_input = {
        "limit": LIMIT,
        "fromDate": iso(from_dt),
        "toDate": iso(to_dt),
        "includeKeywords.keywords": KEYWORDS,
        "includeKeywords.matchTitle": True,
        "includeKeywords.matchSkills": True,
        "includeKeywords.matchDescription": False,
        # --- Aapki sharten (actor ke server pe, reject hui jobs ke paise nahi) ---
        "client.paymentMethodVerified": True,
        "budget.minClientHireRate": MIN_HIRE_RATE + 1,
        "budget.noHireRate": False,
        "budget.fixedPrice.min": str(MIN_FIXED),
        "client.hireHistory": ["UP_TO", "MORE_THAN"],   # jinke 0 hires hain woh bahar
        # --- Form ki pehle se bhari limits khol di (koi achhi job na kate) ---
        "budget.fixedPrice.max": "10000000",
        "budget.hourlyRate.min": "0",
        "budget.hourlyRate.max": "100000",
        "budget.avgHourlyRate.min": "0",
        "budget.avgHourlyRate.max": "100000",
        "budget.allowUnspecifiedBudget": True,   # hourly jobs jin mein rate nahi likha
        "budget.noAvgHourlyRatePaid": True,      # clients jin ki hourly history nahi
        "budget.onlyContractToHire": False,
        "budget.hourlyWorkloads": ["UNSPECIFIED", "LESS_THAN_30_HOURS", "MORE_THAN_30_HOURS"],
        "budget.jobDurations": ["UNSPECIFIED", "UP_TO_ONE_MONTH", "UP_TO_THREE_MONTHS",
                                "UP_TO_SIX_MONTHS", "MORE_THAN_SIX_MONTHS"],
        "client.companySizeRange": ["UNSPECIFIED", "SOLO_ENTERPRENEUR", "UP_TO_10_EMPLOYEES",
                                    "UP_TO_100_EMPOLOYEES", "UP_TO_500_EMPLOYEES",
                                    "UP_TO_1K_EMPLOYEES", "MORE_THAN_1K_EMPLOYEES"],
        "client.includeWithNoFeedback": True,
        "client.phoneNumberVerified": False,
        "vendor.excludeWithQuestions": False,
        "vendor.includeFeatured": False,
        "vendor.includeWithoutCountryPreference": True,
        "addons.enableClientActivity": False,
        "addons.enableClientDetails": False,
        "addons.enableJobAttachments": False,
    }
    # Pehli koshish: categories aur connects ki limit bhi hata do.
    # Agar actor ye values qubool na kare to doosri koshish in ke baghair.
    attempts = [
        dict(base_input, **{"jobCategories": [],
                            "budget.connectsPrice.min": 0,
                            "budget.connectsPrice.max": 100}),
        dict(base_input, **{"budget.connectsPrice.min": 1,
                            "budget.connectsPrice.max": 50}),
        base_input,
    ]
    print(f"Window: {iso(from_dt)} -> {iso(to_dt)}")
    run = None
    for i, run_input in enumerate(attempts, 1):
        try:
            run = client.actor(ACTOR_ID).call(run_input=run_input, timeout_secs=300)
        except Exception as ex:
            print(f"Koshish {i} fail (input reject?): {ex}")
            continue
        if run_ok(run):
            print(f"Koshish {i} kamyab")
            break
        print(f"Koshish {i} run status: {field(run, 'status')}")
    if not run_ok(run):
        raise RuntimeError("Actor run teeno koshishon mein fail hua")
    dataset_id = field(run, "defaultDatasetId", "default_dataset_id")
    items = field(client.dataset(dataset_id).list_items(), "items") or []
    items = [i if isinstance(i, dict) else (i.model_dump() if hasattr(i, "model_dump") else dict(i))
             for i in items]
    print(f"Actor ne {len(items)} jobs di")
    if len(items) >= LIMIT:
        slack_text(f"⚠️ Limit ({LIMIT}) poori ho gayi, kuch jobs miss ho sakti hain.")

    for job in sorted(items, key=lambda j: j.get("createdAt", "")):
        uid = job.get("uid")
        if uid in seen:
            continue
        ok, info = check(job)
        if not ok:
            print(f"Sharten fail: {job.get('title', '')[:50]} ({info})")
        else:
            try:
                ai = ask_ai(job_to_text(job, info))
            except Exception as ex:
                print("AI error:", ex)
                slack_job([f"⚠️ AI error, khud dekh lein: *{esc(job.get('title', ''))}*"],
                          job.get("externalLink"))
                ai = None
            if ai and ai.get("apply"):
                notify(job, info, ai)
            elif ai:
                print(f"AI skip: {job.get('title', '')[:50]} ({ai.get('reason')})")
                if SEND_SKIPPED:
                    slack_job([f"⏭ Skipped: *{esc(job.get('title', ''))}*",
                               esc(ai.get("reason", ""))], job.get("externalLink"))
        seen.append(uid)
        kv.set_record("STATE", {"cursor": state.get("cursor"), "seen": seen[-1000:]})

    kv.set_record("STATE", {"cursor": iso(to_dt), "seen": seen[-1000:]})
    print("Done")


if __name__ == "__main__":
    main()
