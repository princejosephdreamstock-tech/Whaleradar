"""
Whale Radar — Flask Backend
Serves the dashboard and runs the scraper as a background job.
"""

import os
import json
import threading
import hashlib
import re
import time
import logging
import sys
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file
import requests as req
from bs4 import BeautifulSoup
import pandas as pd

app = Flask(__name__)

# ── State (in-memory, survives the request cycle) ──────────────
job_state = {
    "running":   False,
    "progress":  0,
    "total":     0,
    "current":   "",
    "log":       [],
    "leads":     [],
    "started":   None,
    "finished":  None,
    "error":     None,
}
state_lock = threading.Lock()

OUTPUT_FILE     = "/tmp/whale_radar_results.csv"
CHECKPOINT_FILE = "/tmp/whale_radar_checkpoint.json"
SLEEP           = 2
REQUEST_TIMEOUT = 12

# ── Logging to both console and job log ────────────────────────
def jlog(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    with state_lock:
        job_state["log"].append(entry)
        if len(job_state["log"]) > 500:
            job_state["log"] = job_state["log"][-500:]
    if level == "ERROR":
        app.logger.error(msg)
    else:
        app.logger.info(msg)


# ══════════════════════════════════════════════════════
# SCRAPER CORE (same logic as whale_radar.py)
# ══════════════════════════════════════════════════════

AI_SIGNAL_KEYWORDS = [
    "Microsoft Copilot","Copilot for Microsoft 365","Harvey AI","Harvey",
    "Generative AI","GenAI","ChatGPT","GPT-4","OpenAI","Clio Duo",
    "Lexis+ AI","Lexis AI","Thomson Reuters CoCounsel","CoCounsel",
    "Luminance","Kira Systems","Kira","Spellbook","ROSS Intelligence",
    "Westlaw AI","AI-assisted","AI-powered","AI-driven",
    "large language model","LLM","legal AI","contract review AI",
]

AI_SCAN_PATHS   = ["news","insights","blog","updates","technology",
                   "innovation","thought-leadership","press","knowledge","resources"]
PEOPLE_PATHS    = ["team","our-team","people","our-people","lawyers",
                   "attorneys","partners","solicitors","professionals","who-we-are"]
TARGET_TITLES   = ["Managing Partner","Head of Professional Indemnity","Head of PI",
                   "Professional Indemnity Partner","Insurance Partner",
                   "Head of Insurance","Senior Partner","Equity Partner"]
DISCOVERY_SOURCES = [
    "https://www.legal500.com/practice-areas/insurance/",
    "https://www.legal500.com/practice-areas/professional-negligence/",
    "https://www.thelawyer.com/top-law-firms/",
    "https://www.avvo.com/insurance-lawyer.html",
    "https://www.avvo.com/professional-liability-lawyer.html",
    "https://www.martindale.com/by-location/london-england-lawyers/",
    "https://www.martindale.com/by-location/new-york-new-york-lawyers/",
]
SEED_FIRMS = [
    ("Weightmans LLP","weightmans.com"),("Kennedys Law","kennedys-law.com"),
    ("DAC Beachcroft","dacbeachcroft.com"),("Clyde & Co","clydeco.com"),
    ("BLM Law","blmlaw.net"),("Horwich Farrelly","horwichfarrelly.co.uk"),
    ("Reynolds Porter Chamberlain","rpc.co.uk"),("Browne Jacobson","brownejacobson.com"),
    ("Farrer & Co","farrer.co.uk"),("Penningtons Manches Cooper","penningtons.co.uk"),
    ("Trowers & Hamlins","trowers.com"),("Ince & Co","incegd.com"),
    ("Lewis Brisbois","lewisbrisbois.com"),("Wilson Elser","wilsonelser.com"),
    ("Kaufman Dolowich","kdvlaw.com"),("Seyfarth Shaw","seyfarth.com"),
    ("Cozen O'Connor","cozen.com"),("Goldberg Segalla","goldbergsegalla.com"),
    ("Hanson Bridgett","hansonbridgett.com"),("Hinshaw & Culbertson","hinshawlaw.com"),
    ("Tressler LLP","tresslerllp.com"),("Fishburns Solicitors","fishburns.co.uk"),
    ("Beale & Company","beale-law.com"),("Plexus Law","plexuslaw.co.uk"),
    ("Clyde & Co US","clydeco.us"),
]

scraper_session = req.Session()
scraper_session.headers.update({
    "User-Agent": "WhaleRadar/2.0 (EU AI Act compliance research; ethical bot)",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
})

EMAIL_RE = re.compile(r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,6}")
PHONE_UK = re.compile(r"(\+44[\s\-.]?|0)[\d\s\-\.]{9,14}")
PHONE_US = re.compile(r"(\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}")

def sget(url, label=""):
    try:
        r = scraper_session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return r if r.status_code == 200 else None
    except:
        return None

def norm(domain):
    domain = domain.strip().rstrip("/")
    return ("https://" + domain) if not domain.startswith("http") else domain

def dom_key(url):
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path
    return re.sub(r"^www\.", "", host.lower()).split("/")[0].split("?")[0]

def guess_domain(name):
    clean = re.sub(r"\b(llp|ltd|limited|llc|inc|plc|and|solicitors|law|legal|co|group|associates|partners|&)\b","",name,flags=re.I)
    clean = re.sub(r"[^a-zA-Z0-9\s]","",clean).strip()
    slug  = "-".join(clean.lower().split())
    return f"{slug}.com" if slug else ""

def is_target(title):
    if not title: return False
    t = title.lower()
    return any(tt.lower() in t for tt in TARGET_TITLES)

def clean_el(el):
    if not el: return ""
    return re.sub(r"\s+"," ",el.get_text(separator=" ")).strip()

def get_email(el):
    if hasattr(el,"find"):
        a = el.find("a", href=re.compile(r"^mailto:",re.I))
        if a: return a["href"].replace("mailto:","").split("?")[0].strip()
        text = el.get_text()
    else: text = str(el)
    m = EMAIL_RE.search(text)
    return m.group(0).strip() if m else ""

def get_phone(el):
    if hasattr(el,"find"):
        a = el.find("a", href=re.compile(r"^tel:",re.I))
        if a: return a["href"].replace("tel:","").strip()
        text = el.get_text()
    else: text = str(el)
    m = PHONE_UK.search(text) or PHONE_US.search(text)
    return re.sub(r"\s+"," ",m.group(0)).strip() if m else ""

def get_name(card):
    for tag in ["h1","h2","h3","h4"]:
        el = card.find(tag)
        if el:
            t = clean_el(el)
            w = t.split()
            if 2 <= len(w) <= 5 and len(t) < 60: return t
    el = card.find(class_=re.compile(r"\bname\b",re.I))
    if el:
        t = clean_el(el)
        if 2 <= len(t.split()) <= 5: return t
    return ""

def get_title(card):
    for pat in [r"\btitle\b",r"\brole\b",r"\bposition\b"]:
        el = card.find(class_=re.compile(pat,re.I))
        if el:
            t = clean_el(el)
            if t: return t
    for el in card.find_all(["p","span","div"]):
        t = clean_el(el)
        if is_target(t) and len(t) < 80: return t
    return ""

def detect_ai(html):
    soup = BeautifulSoup(html,"lxml")
    for tag in soup(["script","style","noscript"]): tag.decompose()
    text = soup.get_text(separator=" ")
    specific = [k for k in AI_SIGNAL_KEYWORDS if len(k.split())>1]
    generic  = [k for k in AI_SIGNAL_KEYWORDS if len(k.split())==1]
    for kw in specific+generic:
        if kw.lower() in text.lower(): return kw
    return ""

def scan_ai(base_url, name):
    for path in AI_SCAN_PATHS:
        for url in [f"{base_url}/{path}/",f"{base_url}/{path}"]:
            r = sget(url,name); time.sleep(SLEEP)
            if r:
                tool = detect_ai(r.text)
                if tool:
                    jlog(f"  🤖 AI signal: '{tool}' at {url}")
                    return tool, url
    r = sget(base_url,name)
    if r:
        tool = detect_ai(r.text)
        if tool: return tool, base_url
    return "",""

def scrape_people(url, firm):
    r = sget(url,firm); time.sleep(SLEEP)
    if not r: return []
    soup = BeautifulSoup(r.text,"lxml")
    for tag in soup(["nav","footer","header","aside"]): tag.decompose()
    card_cls = re.compile(r"\b(team|people|person|attorney|lawyer|partner|solicitor|professional|member|profile|staff|bio)\b",re.I)
    cards = soup.find_all(["div","article","li","section"], class_=card_cls)
    if len(cards)<2:
        for el in soup.find_all(["p","span","div","td"]):
            t = clean_el(el)
            if is_target(t) and 5<len(t)<80:
                parent=el.parent
                for _ in range(4):
                    if parent and len(parent.get_text())>50:
                        cards.append(parent); break
                    parent=parent.parent if parent else None
    seen,contacts=[],[]
    for card in cards:
        name=get_name(card); title=get_title(card)
        email=get_email(card); phone=get_phone(card)
        if not name or name in seen: continue
        if not is_target(title): continue
        seen.append(name)
        contacts.append({"name":name,"title":title,"email":email,"phone":phone})
    jlog(f"  👤 {len(contacts)} contacts at {url}")
    return contacts

def scrape_contacts(base_url, firm):
    for path in PEOPLE_PATHS:
        for url in [f"{base_url}/{path}/",f"{base_url}/{path}"]:
            c = scrape_people(url,firm)
            if c: return c
    return []

def discover_firms(url, max_per=150):
    jlog(f"Discovering firms from {url}")
    r = sget(url,"discover"); time.sleep(SLEEP)
    if not r: return []
    soup = BeautifulSoup(r.text,"lxml")
    firms,seen=[],set()
    for a in soup.find_all("a",href=True):
        href=a["href"].strip(); text=a.get_text(strip=True)
        if not href.startswith("http"): continue
        if any(s in href for s in ["legal500","chambers","martindale","avvo","thelawyer","google","twitter","linkedin","facebook"]): continue
        if len(text)<3 or len(text)>80: continue
        d = dom_key(href)
        if d and d not in seen:
            seen.add(d)
            fname = text if re.search(r"(law|legal|solicitor|llp|llc|&)",text,re.I) else d.replace(".com","").replace(".co.uk","").replace("-"," ").title()
            firms.append((fname.strip(),d))
        if len(firms)>=max_per: break
    jlog(f"  Found {len(firms)} firms from {url}")
    return firms

_hook_c={}
_WITH_AI=[
    "I noticed {firm} is integrating {tool} across {role} workflows; under EU AI Act Article 14, this specific deployment requires a documented human oversight protocol and forensic audit trail before August 2nd, 2026.",
    "Given {firm}'s adoption of {tool}, Article 14 of the EU AI Act mandates that your team establish a verifiable human review mechanism for AI-assisted decisions — the compliance window closes August 2nd, 2026.",
    "{firm}'s use of {tool} in {role} creates a direct Article 14 exposure: the EU AI Act requires firms to maintain real-time oversight logs for high-risk AI workflows by August 2nd, 2026.",
]
_NO_AI=[
    "As {firm} expands its legal technology capabilities, Article 14 of the EU AI Act mandates that any AI systems used in professional indemnity workflows require a verified human oversight audit by August 2nd, 2026.",
    "With the EU AI Act Article 14 deadline of August 2nd, 2026 approaching, {firm}'s {role} practice may need to demonstrate documented AI oversight protocols for any AI-assisted legal processes currently in use.",
]

def gen_hook(firm,tool,title):
    role=(title or "legal").lower()
    k="y" if tool else "n"
    i=_hook_c.get(k,0); _hook_c[k]=i+1
    tpl=(_WITH_AI if tool else _NO_AI)[i%(len(_WITH_AI) if tool else len(_NO_AI))]
    return tpl.format(firm=firm,tool=tool,role=role)

def contact_key(name,firm):
    return hashlib.md5(f"{name.strip().lower()}|{firm.strip().lower()}".encode()).hexdigest()

def load_checkpoint():
    if not os.path.exists(CHECKPOINT_FILE): return set()
    try:
        with open(CHECKPOINT_FILE) as f: return set(json.load(f).get("processed",[]))
    except: return set()

def save_checkpoint(domains):
    with open(CHECKPOINT_FILE,"w") as f:
        json.dump({"processed":sorted(domains),"updated":datetime.now().isoformat()},f)

def load_existing_keys():
    if not os.path.exists(OUTPUT_FILE): return set()
    try:
        df=pd.read_csv(OUTPUT_FILE,usecols=["Name","Firm"])
        return {contact_key(r["Name"],r["Firm"]) for _,r in df.iterrows()}
    except: return set()

def append_csv(rows,existing_keys):
    if not rows: return 0
    fresh=[r for r in rows if contact_key(r["Name"],r["Firm"]) not in existing_keys]
    for r in fresh: existing_keys.add(contact_key(r["Name"],r["Firm"]))
    if not fresh: return 0
    df=pd.DataFrame(fresh)
    file_exists=os.path.exists(OUTPUT_FILE)
    df.to_csv(OUTPUT_FILE,mode="a",header=not file_exists,index=False,encoding="utf-8-sig")
    return len(fresh)


# ══════════════════════════════════════════════════════
# BACKGROUND JOB
# ══════════════════════════════════════════════════════

def run_scraper_job(max_firms=99999):
    with state_lock:
        job_state["running"]=True
        job_state["progress"]=0
        job_state["log"]=[]
        job_state["leads"]=[]
        job_state["error"]=None
        job_state["started"]=datetime.now().isoformat()
        job_state["finished"]=None

    try:
        processed = load_checkpoint()
        existing_keys = load_existing_keys()

        # Build firm list
        jlog("Building firm list from discovery sources...")
        all_firms=[]
        seen_domains=set()

        def add_firm(name,domain):
            if not domain: return
            d=dom_key(norm(domain))
            if d and d not in seen_domains:
                seen_domains.add(d)
                all_firms.append((name.strip(),domain.strip()))

        for src in DISCOVERY_SOURCES:
            for name,domain in discover_firms(src):
                add_firm(name,domain)
                if len(all_firms)>=max_firms: break
            if len(all_firms)>=max_firms: break

        for name,domain in SEED_FIRMS:
            add_firm(name,domain)

        pending=[(n,d) for n,d in all_firms if dom_key(norm(d)) not in processed]

        with state_lock:
            job_state["total"]=len(pending)

        jlog(f"Total firms: {len(all_firms)} | Already done: {len(all_firms)-len(pending)} | To scrape: {len(pending)}")

        total_written=0

        for i,(firm_name,domain) in enumerate(pending):
            with state_lock:
                if not job_state["running"]: break  # stopped by user
                job_state["progress"]=i+1
                job_state["current"]=firm_name

            base_url=norm(domain)
            jlog(f"[{i+1}/{len(pending)}] {firm_name} ({base_url})")

            ai_tool,ai_page=scan_ai(base_url,firm_name)
            contacts=scrape_contacts(base_url,firm_name)

            if not contacts:
                contacts=[{"name":"Managing Partner (not scraped)","title":"Managing Partner","email":"","phone":""}]

            rows=[]
            for c in contacts:
                row={
                    "Name":c.get("name",""),
                    "Title":c.get("title",""),
                    "Firm":firm_name,
                    "Website":base_url,
                    "Email":c.get("email",""),
                    "Phone":c.get("phone",""),
                    "AI Tool Mentioned":ai_tool,
                    "AI Signal Page":ai_page,
                    "Forensic Hook":gen_hook(firm_name,ai_tool,c.get("title","")),
                    "Date Scraped":datetime.today().strftime("%Y-%m-%d"),
                }
                rows.append(row)

            written=append_csv(rows,existing_keys)
            total_written+=written

            # Push new leads to state for live dashboard
            with state_lock:
                for r in rows:
                    job_state["leads"].append(r)

            processed.add(dom_key(base_url))
            save_checkpoint(processed)

        jlog(f"✅ COMPLETE — {total_written} new contacts written to CSV")

    except Exception as e:
        with state_lock:
            job_state["error"]=str(e)
        jlog(f"ERROR: {e}","ERROR")
    finally:
        with state_lock:
            job_state["running"]=False
            job_state["finished"]=datetime.now().isoformat()


# ══════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/start", methods=["POST"])
def start_job():
    with state_lock:
        if job_state["running"]:
            return jsonify({"error":"Job already running"}), 400
    max_firms = request.json.get("max_firms", 99999) if request.is_json else 99999
    t = threading.Thread(target=run_scraper_job, args=(max_firms,), daemon=True)
    t.start()
    return jsonify({"status":"started"})

@app.route("/api/stop", methods=["POST"])
def stop_job():
    with state_lock:
        job_state["running"]=False
    return jsonify({"status":"stopped"})

@app.route("/api/reset", methods=["POST"])
def reset_job():
    with state_lock:
        if job_state["running"]:
            return jsonify({"error":"Stop the job first"}), 400
        job_state.update({"progress":0,"total":0,"current":"","log":[],"leads":[],"error":None,"started":None,"finished":None})
    for f in [OUTPUT_FILE, CHECKPOINT_FILE]:
        if os.path.exists(f): os.remove(f)
    return jsonify({"status":"reset"})

@app.route("/api/status")
def status():
    with state_lock:
        return jsonify({
            "running":   job_state["running"],
            "progress":  job_state["progress"],
            "total":     job_state["total"],
            "current":   job_state["current"],
            "log":       job_state["log"][-50:],
            "leads":     job_state["leads"],
            "error":     job_state["error"],
            "started":   job_state["started"],
            "finished":  job_state["finished"],
        })

@app.route("/api/download")
def download():
    if not os.path.exists(OUTPUT_FILE):
        return jsonify({"error":"No results yet"}), 404
    return send_file(OUTPUT_FILE, as_attachment=True, download_name="whale_radar_results.csv")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
