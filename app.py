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

from flask import Flask, jsonify, request, send_file
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

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WHALE RADAR — EU AI Act Lead Engine</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root {
  --ink: #0a0a0f; --paper: #f5f2eb; --accent: #ff4d1c; --gold: #c9a84c;
  --steel: #1a1a2e; --mist: #e8e4da; --signal: #00e5a0; --warn: #ffb800;
  --dim: #6b6860;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--ink);color:var(--paper);font-family:'Syne',sans-serif;min-height:100vh;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.04'/%3E%3C/svg%3E");pointer-events:none;z-index:1000;opacity:.6}

/* HEADER */
header{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;padding:1.5rem 2.5rem;border-bottom:1px solid rgba(255,255,255,.08)}
.logo-mark{width:40px;height:40px;background:var(--accent);clip-path:polygon(50% 0%,100% 25%,100% 75%,50% 100%,0% 75%,0% 25%);display:flex;align-items:center;justify-content:center;font-size:16px;animation:pulse-hex 3s ease-in-out infinite}
@keyframes pulse-hex{0%,100%{box-shadow:0 0 0 0 rgba(255,77,28,0)}50%{box-shadow:0 0 0 12px rgba(255,77,28,.15)}}
.header-left{display:flex;align-items:center;gap:.8rem}
.brand{font-size:.6rem;letter-spacing:.25em;text-transform:uppercase;color:var(--dim);font-family:'Space Mono',monospace}
.title-center h1{font-size:clamp(1.2rem,2.5vw,1.8rem);font-weight:800;letter-spacing:-.02em;text-align:center}
.title-center h1 span{color:var(--accent)}
.header-right{display:flex;justify-content:flex-end;align-items:center;gap:.75rem}
.deadline-pill{background:rgba(255,184,0,.12);border:1px solid rgba(255,184,0,.3);border-radius:100px;padding:.35rem .9rem;font-family:'Space Mono',monospace;font-size:.6rem;color:var(--warn);display:flex;align-items:center;gap:.4rem}
.deadline-pill::before{content:'';width:5px;height:5px;background:var(--warn);border-radius:50%;animation:blink 1.2s ease infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}

/* STATS */
.stats-bar{display:grid;grid-template-columns:repeat(5,1fr);border-bottom:1px solid rgba(255,255,255,.06)}
.stat{padding:1.2rem 2rem;border-right:1px solid rgba(255,255,255,.06);position:relative;overflow:hidden}
.stat:last-child{border-right:none}
.stat-value{font-size:2.4rem;font-weight:800;line-height:1;letter-spacing:-.04em;margin-bottom:.25rem}
.stat-value.accent{color:var(--accent)}.stat-value.signal{color:var(--signal)}.stat-value.gold{color:var(--gold)}.stat-value.warn{color:var(--warn)}
.stat-label{font-family:'Space Mono',monospace;font-size:.55rem;letter-spacing:.2em;text-transform:uppercase;color:var(--dim)}
.stat-bg{position:absolute;right:-5px;bottom:-5px;font-size:4rem;opacity:.04;font-weight:800;pointer-events:none;line-height:1}

/* CONTROLS */
.controls{display:flex;align-items:center;gap:1rem;padding:1.2rem 2.5rem;border-bottom:1px solid rgba(255,255,255,.06);background:rgba(255,255,255,.02)}
.btn{border:none;border-radius:6px;padding:.7rem 1.5rem;font-family:'Syne',sans-serif;font-weight:700;font-size:.8rem;cursor:pointer;display:flex;align-items:center;gap:.5rem;transition:all .2s;letter-spacing:.02em}
.btn-start{background:var(--signal);color:#000}.btn-start:hover{background:#00ffb3;transform:translateY(-1px);box-shadow:0 6px 20px rgba(0,229,160,.3)}
.btn-stop{background:rgba(255,77,28,.15);color:var(--accent);border:1px solid rgba(255,77,28,.3)}.btn-stop:hover{background:rgba(255,77,28,.25)}
.btn-reset{background:rgba(255,255,255,.06);color:var(--dim);border:1px solid rgba(255,255,255,.1)}.btn-reset:hover{background:rgba(255,255,255,.1);color:var(--paper)}
.btn-download{background:var(--gold);color:#000;margin-left:auto}.btn-download:hover{background:#e0ba5a;transform:translateY(-1px)}
.btn:disabled{opacity:.4;cursor:not-allowed;transform:none!important}
.max-firms-wrap{display:flex;align-items:center;gap:.5rem;font-family:'Space Mono',monospace;font-size:.65rem;color:var(--dim)}
.max-firms-wrap input{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:4px;padding:.4rem .6rem;color:var(--paper);font-family:'Space Mono',monospace;font-size:.65rem;width:80px;outline:none}
.max-firms-wrap input:focus{border-color:var(--accent)}

/* PROGRESS BAR */
.progress-bar-wrap{height:3px;background:rgba(255,255,255,.06);position:relative;overflow:hidden}
.progress-bar{height:100%;background:linear-gradient(90deg,var(--accent),var(--warn));transition:width .5s ease;width:0%}
.progress-bar.pulse{animation:bar-pulse 1.5s ease-in-out infinite}
@keyframes bar-pulse{0%,100%{opacity:1}50%{opacity:.5}}

/* MAIN */
.main{display:grid;grid-template-columns:340px 1fr;min-height:calc(100vh - 260px)}

/* LOG PANEL */
.log-panel{border-right:1px solid rgba(255,255,255,.06);display:flex;flex-direction:column}
.panel-title{font-family:'Space Mono',monospace;font-size:.58rem;letter-spacing:.25em;text-transform:uppercase;color:var(--dim);padding:.9rem 1.5rem;border-bottom:1px solid rgba(255,255,255,.06);display:flex;align-items:center;gap:.5rem}
.status-dot{width:6px;height:6px;border-radius:50%;background:var(--dim);flex-shrink:0}
.status-dot.running{background:var(--signal);animation:blink .8s ease infinite}
.log-list{flex:1;overflow-y:auto;padding:.75rem;font-family:'Space Mono',monospace;font-size:.62rem;line-height:1.7;color:var(--dim);display:flex;flex-direction:column;gap:.1rem;max-height:calc(100vh - 320px)}
.log-entry{padding:.2rem .4rem;border-radius:3px;word-break:break-all}
.log-entry.ai{color:var(--signal);background:rgba(0,229,160,.05)}
.log-entry.contact{color:var(--gold);background:rgba(201,168,76,.05)}
.log-entry.complete{color:var(--warn);font-weight:700}
.log-entry.error{color:var(--accent);background:rgba(255,77,28,.08)}
.log-empty{display:flex;align-items:center;justify-content:center;height:100%;color:var(--dim);font-family:'Space Mono',monospace;font-size:.65rem;text-align:center;padding:2rem}

/* RESULTS PANEL */
.results-panel{display:flex;flex-direction:column}
.table-header{display:grid;grid-template-columns:170px 160px 90px 120px 1fr 36px;gap:.75rem;padding:.9rem 1.5rem;border-bottom:1px solid rgba(255,255,255,.06);font-family:'Space Mono',monospace;font-size:.55rem;letter-spacing:.15em;text-transform:uppercase;color:var(--dim)}
.leads-list{flex:1;overflow-y:auto;max-height:calc(100vh - 340px)}
.lead-row{display:grid;grid-template-columns:170px 160px 90px 120px 1fr 36px;gap:.75rem;padding:1rem 1.5rem;border-bottom:1px solid rgba(255,255,255,.04);align-items:center;cursor:pointer;transition:background .15s;animation:row-in .3s ease both}
.lead-row:hover{background:rgba(255,255,255,.03)}
.lead-row.selected{background:rgba(255,77,28,.06);border-left:2px solid var(--accent)}
@keyframes row-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.lead-name{font-weight:700;font-size:.8rem;line-height:1.2}
.lead-title{font-family:'Space Mono',monospace;font-size:.58rem;color:var(--dim);margin-top:.1rem}
.lead-firm{font-size:.78rem;color:var(--mist)}
.ai-badge{display:inline-flex;align-items:center;gap:.25rem;background:rgba(0,229,160,.1);border:1px solid rgba(0,229,160,.25);border-radius:3px;padding:.2rem .4rem;font-family:'Space Mono',monospace;font-size:.55rem;color:var(--signal);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.ai-badge.none{background:rgba(255,255,255,.04);border-color:rgba(255,255,255,.08);color:var(--dim)}
.email-cell{font-family:'Space Mono',monospace;font-size:.6rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hook-preview{font-size:.68rem;color:var(--dim);line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.expand-btn{width:26px;height:26px;border-radius:50%;background:rgba(255,255,255,.06);border:none;color:var(--dim);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:.7rem;transition:all .15s;flex-shrink:0}
.expand-btn:hover{background:var(--accent);color:#fff}
.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:4rem;text-align:center;gap:1rem;color:var(--dim);height:200px}
.empty-icon{font-size:2.5rem;opacity:.3}

/* DRAWER */
.drawer-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(4px);z-index:100;opacity:0;pointer-events:none;transition:opacity .3s}
.drawer-overlay.open{opacity:1;pointer-events:all}
.drawer{position:fixed;right:0;top:0;bottom:0;width:460px;background:#111118;border-left:1px solid rgba(255,255,255,.1);z-index:101;transform:translateX(100%);transition:transform .35s cubic-bezier(.4,0,.2,1);display:flex;flex-direction:column;overflow-y:auto}
.drawer.open{transform:translateX(0)}
.drawer-top{padding:1.5rem 2rem;border-bottom:1px solid rgba(255,255,255,.08);position:relative}
.drawer-close{position:absolute;top:1.25rem;right:1.25rem;width:28px;height:28px;background:rgba(255,255,255,.06);border:none;border-radius:50%;color:var(--paper);cursor:pointer;font-size:.9rem;display:flex;align-items:center;justify-content:center;transition:background .15s}
.drawer-close:hover{background:var(--accent)}
.drawer-name{font-size:1.4rem;font-weight:800;letter-spacing:-.02em;margin-bottom:.25rem}
.drawer-sub{font-family:'Space Mono',monospace;font-size:.65rem;color:var(--gold)}
.drawer-body{padding:1.5rem 2rem;display:flex;flex-direction:column;gap:1.25rem}
.dsec-title{font-family:'Space Mono',monospace;font-size:.55rem;letter-spacing:.2em;text-transform:uppercase;color:var(--dim);margin-bottom:.6rem}
.info-grid{display:grid;grid-template-columns:1fr 1fr;gap:.6rem}
.info-cell{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:5px;padding:.65rem}
.info-label{font-family:'Space Mono',monospace;font-size:.5rem;letter-spacing:.15em;text-transform:uppercase;color:var(--dim);margin-bottom:.25rem}
.info-val{font-size:.75rem;font-weight:600;word-break:break-all}
.hook-box{background:rgba(201,168,76,.08);border:1px solid rgba(201,168,76,.2);border-radius:7px;padding:1.1rem}
.hook-text{font-size:.78rem;line-height:1.6;color:var(--mist)}
.hook-text strong{color:var(--gold)}
.copy-btn{margin-top:.6rem;background:rgba(201,168,76,.15);border:1px solid rgba(201,168,76,.25);border-radius:4px;padding:.45rem .9rem;font-family:'Space Mono',monospace;font-size:.6rem;color:var(--gold);cursor:pointer;transition:all .15s;letter-spacing:.1em}
.copy-btn:hover{background:rgba(201,168,76,.25)}
.copy-btn.copied{color:var(--signal);border-color:var(--signal);background:rgba(0,229,160,.1)}

/* TICKER */
.ticker{border-top:1px solid rgba(255,255,255,.06);background:rgba(255,77,28,.04);padding:.5rem 2rem;font-family:'Space Mono',monospace;font-size:.58rem;color:var(--dim);white-space:nowrap;overflow:hidden}
.ticker-inner{display:inline-block;animation:ticker-scroll 50s linear infinite}
@keyframes ticker-scroll{from{transform:translateX(100vw)}to{transform:translateX(-100%)}}

::-webkit-scrollbar{width:3px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:2px}
</style>
</head>
<body>

<header>
  <div class="header-left">
    <div class="logo-mark">🐋</div>
    <div>
      <div style="font-size:1rem;font-weight:800;letter-spacing:-.02em">WHALE RADAR</div>
      <div class="brand">EU AI Act · Lead Engine v2</div>
    </div>
  </div>
  <div class="title-center"><h1>B2B Lead <span>Intelligence</span></h1></div>
  <div class="header-right">
    <div class="deadline-pill">ART. 14 DEADLINE: AUG 2, 2026</div>
  </div>
</header>

<div class="stats-bar">
  <div class="stat"><div class="stat-value accent" id="s-total">0</div><div class="stat-label">Total Leads</div><div class="stat-bg">W</div></div>
  <div class="stat"><div class="stat-value signal" id="s-ai">0</div><div class="stat-label">AI Signal</div><div class="stat-bg">AI</div></div>
  <div class="stat"><div class="stat-value gold" id="s-email">0</div><div class="stat-label">Emails Found</div><div class="stat-bg">@</div></div>
  <div class="stat"><div class="stat-value warn" id="s-progress">0</div><div class="stat-label">Firms Scraped</div><div class="stat-bg">F</div></div>
  <div class="stat"><div class="stat-value" id="s-days">—</div><div class="stat-label">Days to Deadline</div><div class="stat-bg">D</div></div>
</div>

<div class="controls">
  <button class="btn btn-start" id="btn-start" onclick="startJob()">▶ Start Scraping</button>
  <button class="btn btn-stop"  id="btn-stop"  onclick="stopJob()"  disabled>■ Stop</button>
  <button class="btn btn-reset" id="btn-reset" onclick="resetJob()">↺ Reset</button>
  <div class="max-firms-wrap">
    Max firms: <input type="number" id="max-firms" value="500" min="10" max="99999">
  </div>
  <button class="btn btn-download" id="btn-dl" onclick="downloadCSV()" disabled>↓ Download CSV</button>
</div>

<div class="progress-bar-wrap">
  <div class="progress-bar" id="progress-bar"></div>
</div>

<div class="main">
  <div class="log-panel">
    <div class="panel-title">
      <span class="status-dot" id="status-dot"></span>
      Live Activity Log
    </div>
    <div class="log-list" id="log-list">
      <div class="log-empty">Press ▶ Start to begin scraping</div>
    </div>
  </div>

  <div class="results-panel">
    <div class="table-header">
      <span>Contact</span><span>Firm</span><span>AI Tool</span>
      <span>Email</span><span>Forensic Hook</span><span></span>
    </div>
    <div class="leads-list" id="leads-list">
      <div class="empty-state">
        <div class="empty-icon">🐋</div>
        <div style="font-family:'Space Mono',monospace;font-size:.65rem">Leads will appear here in real time</div>
      </div>
    </div>
  </div>
</div>

<div class="ticker"><div class="ticker-inner" id="ticker">WHALE RADAR · EU AI Act Article 14 · August 2, 2026 Deadline · Managing Partners · Heads of Professional Indemnity · UK & US Law Firms · Harvey AI · Microsoft Copilot · Luminance · Real-Time Lead Intelligence</div></div>

<div class="drawer-overlay" id="overlay" onclick="closeDrawer()"></div>
<div class="drawer" id="drawer">
  <div class="drawer-top">
    <button class="drawer-close" onclick="closeDrawer()">✕</button>
    <div class="drawer-name" id="d-name">—</div>
    <div class="drawer-sub"  id="d-sub">—</div>
  </div>
  <div class="drawer-body">
    <div><div class="dsec-title">Contact Details</div><div class="info-grid" id="d-info"></div></div>
    <div>
      <div class="dsec-title">🎯 Forensic Hook — Copy & Send</div>
      <div class="hook-box">
        <div class="hook-text" id="d-hook"></div>
        <button class="copy-btn" id="copy-btn" onclick="copyHook()">⎘ COPY TO CLIPBOARD</button>
      </div>
    </div>
    <div>
      <div class="dsec-title">EU AI Act Article 14 Context</div>
      <div style="background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:5px;padding:.9rem;font-size:.72rem;line-height:1.6;color:var(--dim)">
        Article 14 requires human oversight mechanisms for high-risk AI systems. Law firms using AI in professional indemnity workflows must document oversight protocols, maintain audit trails, and demonstrate human review capability before <strong style="color:var(--warn)">August 2, 2026</strong>.
      </div>
    </div>
  </div>
</div>

<script>
let leads = [];
let selectedLead = null;
let pollInterval = null;
let seenLeadCount = 0;

// ── Deadline counter ──
(function(){
  const d = Math.ceil((new Date('2026-08-02') - new Date()) / 86400000);
  const el = document.getElementById('s-days');
  el.textContent = d;
  el.className = 'stat-value ' + (d < 90 ? 'accent' : d < 180 ? 'warn' : '');
})();

// ── Job controls ──
async function startJob() {
  const max = parseInt(document.getElementById('max-firms').value) || 500;
  await fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({max_firms: max})});
  document.getElementById('btn-start').disabled = true;
  document.getElementById('btn-stop').disabled  = false;
  document.getElementById('btn-reset').disabled = true;
  document.getElementById('status-dot').className = 'status-dot running';
  document.getElementById('progress-bar').classList.add('pulse');
  document.getElementById('log-list').innerHTML = '';
  startPolling();
}

async function stopJob() {
  await fetch('/api/stop', {method:'POST'});
}

async function resetJob() {
  if (!confirm('Reset everything? This will delete all results and the checkpoint.')) return;
  await fetch('/api/reset', {method:'POST'});
  leads = []; seenLeadCount = 0;
  document.getElementById('leads-list').innerHTML = `<div class="empty-state"><div class="empty-icon">🐋</div><div style="font-family:'Space Mono',monospace;font-size:.65rem">Leads will appear here in real time</div></div>`;
  document.getElementById('log-list').innerHTML = `<div class="log-empty">Press ▶ Start to begin scraping</div>`;
  ['s-total','s-ai','s-email','s-progress'].forEach(id => document.getElementById(id).textContent = '0');
  document.getElementById('progress-bar').style.width = '0%';
  document.getElementById('btn-dl').disabled = true;
  document.getElementById('btn-start').disabled = false;
  document.getElementById('btn-stop').disabled  = true;
  document.getElementById('btn-reset').disabled = false;
}

function downloadCSV() {
  window.location.href = '/api/download';
}

// ── Polling ──
function startPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(poll, 2000);
}

async function poll() {
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    updateUI(data);
    if (!data.running && pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
      onJobFinished(data);
    }
  } catch(e) { console.error('Poll error:', e); }
}

function updateUI(data) {
  // Stats
  animCount('s-total',    data.leads.length);
  animCount('s-ai',       data.leads.filter(l => l['AI Tool Mentioned']).length);
  animCount('s-email',    data.leads.filter(l => l['Email']).length);
  animCount('s-progress', data.progress);

  // Progress bar
  if (data.total > 0) {
    const pct = Math.min(100, (data.progress / data.total) * 100);
    document.getElementById('progress-bar').style.width = pct + '%';
  }

  // Log
  const logList = document.getElementById('log-list');
  const newLogs = data.log;
  if (logList.children.length === 1 && logList.children[0].classList.contains('log-empty')) {
    logList.innerHTML = '';
  }
  // Only append truly new log entries
  const currentCount = logList.children.length;
  if (newLogs.length > currentCount) {
    newLogs.slice(currentCount).forEach(entry => {
      const div = document.createElement('div');
      div.className = 'log-entry' +
        (entry.includes('AI signal') ? ' ai' :
         entry.includes('contact') || entry.includes('👤') ? ' contact' :
         entry.includes('COMPLETE') ? ' complete' :
         entry.includes('ERROR') ? ' error' : '');
      div.textContent = entry;
      logList.appendChild(div);
    });
    logList.scrollTop = logList.scrollHeight;
  }

  // New leads
  if (data.leads.length > seenLeadCount) {
    const newLeads = data.leads.slice(seenLeadCount);
    leads = data.leads;
    seenLeadCount = data.leads.length;
    renderNewLeads(newLeads);

    // Update ticker
    const aiLeads = leads.filter(l => l['AI Tool Mentioned']);
    if (aiLeads.length > 0) {
      document.getElementById('ticker').textContent = aiLeads
        .map(l => `🐋 ${l.Firm} — ${l['AI Tool Mentioned']} detected — Article 14 exposure`)
        .join('   ·   ');
    }

    document.getElementById('btn-dl').disabled = false;
  }
}

function renderNewLeads(newLeads) {
  const list = document.getElementById('leads-list');
  // Remove empty state if present
  if (list.querySelector('.empty-state')) list.innerHTML = '';

  newLeads.forEach((l, i) => {
    const div = document.createElement('div');
    div.className = 'lead-row';
    div.style.animationDelay = (i * 0.05) + 's';
    div.onclick = () => openDrawer(leads.indexOf(l));
    div.innerHTML = `
      <div><div class="lead-name">${esc(l.Name)}</div><div class="lead-title">${esc(l.Title)}</div></div>
      <div class="lead-firm">${esc(l.Firm)}</div>
      <div>${l['AI Tool Mentioned']
        ? `<div class="ai-badge">● ${esc(l['AI Tool Mentioned'])}</div>`
        : `<div class="ai-badge none">— none</div>`}</div>
      <div class="email-cell" style="color:${l.Email ? 'var(--signal)' : 'var(--dim)'}">${l.Email ? '✓ ' + esc(l.Email) : '—'}</div>
      <div class="hook-preview">${esc(l['Forensic Hook'])}</div>
      <button class="expand-btn" onclick="event.stopPropagation();openDrawer(${leads.indexOf(l)})">→</button>
    `;
    list.appendChild(div);
  });
}

function onJobFinished(data) {
  document.getElementById('btn-start').disabled = false;
  document.getElementById('btn-stop').disabled  = true;
  document.getElementById('btn-reset').disabled = false;
  document.getElementById('status-dot').className = 'status-dot';
  document.getElementById('progress-bar').classList.remove('pulse');
  if (data.error) {
    const log = document.getElementById('log-list');
    const div = document.createElement('div');
    div.className = 'log-entry error';
    div.textContent = '❌ Error: ' + data.error;
    log.appendChild(div);
  }
}

// ── Drawer ──
function openDrawer(idx) {
  const l = leads[idx];
  if (!l) return;
  selectedLead = l;

  document.getElementById('d-name').textContent = l.Name;
  document.getElementById('d-sub').textContent  = `${l.Title} · ${l.Firm}`;

  document.getElementById('d-info').innerHTML = `
    <div class="info-cell"><div class="info-label">Email</div><div class="info-val" style="color:${l.Email?'var(--signal)':'var(--dim)'}">${l.Email||'— not found'}</div></div>
    <div class="info-cell"><div class="info-label">Phone</div><div class="info-val">${l.Phone||'— not found'}</div></div>
    <div class="info-cell"><div class="info-label">AI Tool</div><div class="info-val" style="color:${l['AI Tool Mentioned']?'var(--signal)':'var(--dim)'}">${l['AI Tool Mentioned']||'— none detected'}</div></div>
    <div class="info-cell"><div class="info-label">Risk Level</div><div class="info-val" style="color:var(--accent)">${l['AI Tool Mentioned']?'HIGH — Art. 14':'MEDIUM'}</div></div>
  `;

  let h = esc(l['Forensic Hook'])
    .replace(esc(l.Firm), `<strong>${esc(l.Firm)}</strong>`)
    .replace('August 2nd, 2026', '<strong style="color:var(--accent)">August 2nd, 2026</strong>');
  if (l['AI Tool Mentioned']) h = h.replace(esc(l['AI Tool Mentioned']), `<strong>${esc(l['AI Tool Mentioned'])}</strong>`);
  document.getElementById('d-hook').innerHTML = h;

  document.getElementById('copy-btn').textContent = '⎘ COPY TO CLIPBOARD';
  document.getElementById('copy-btn').className = 'copy-btn';
  document.getElementById('drawer').classList.add('open');
  document.getElementById('overlay').classList.add('open');
}

function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('overlay').classList.remove('open');
  selectedLead = null;
}

function copyHook() {
  if (!selectedLead) return;
  navigator.clipboard.writeText(selectedLead['Forensic Hook']).then(() => {
    const btn = document.getElementById('copy-btn');
    btn.textContent = '✓ COPIED!';
    btn.className = 'copy-btn copied';
    setTimeout(() => { btn.textContent = '⎘ COPY TO CLIPBOARD'; btn.className = 'copy-btn'; }, 2000);
  });
}

// ── Utils ──
function esc(str) {
  return String(str||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

const _counts = {};
function animCount(id, target) {
  const el = document.getElementById(id);
  const start = _counts[id] || 0;
  _counts[id] = target;
  if (start === target) return;
  const dur = 400, t0 = performance.now();
  (function step(now) {
    const p = Math.min((now - t0) / dur, 1);
    el.textContent = Math.round(start + (target - start) * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(step);
  })(performance.now());
}

// Poll on load to restore state if job already running
poll();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return DASHBOARD_HTML

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
