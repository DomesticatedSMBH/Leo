from __future__ import annotations

import re, sqlite3, hashlib, json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

TOTO_F1_OUTRIGHTS_URL = "https://sport.toto.nl/wedden/sport/4090/formule-1/outrights"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---- Odds / text helpers
ODDS_DUTCH_RE = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$")
WS_RE = re.compile(r"[ \t\xa0]+")

def wsnorm(s: str) -> str:
    return WS_RE.sub(" ", s.strip())

def parse_dutch_decimal(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))

def implied_probability(d: float) -> float:
    return 0 if not d else 1.0/d

def canonical_key(name: str) -> str:
    k = wsnorm(name).lower()
    k = re.sub(r"[^a-z0-9 ]+", "", k)
    k = re.sub(r"\s+", " ", k).strip()
    for src, tgt in [("áàäâ","a"),("éèëê","e"),("íìïî","i"),("óòöô","o"),("úùüû","u"),("ñ","n"),("ç","c")]:
        for ch in src: k = k.replace(ch, tgt)
    return k

def looks_like_market_title(line: str) -> bool:
    l = wsnorm(line)
    if len(l) > 120:
        return False
    k = l.lower()
    # Block numeric-range selections like "0.10 - 0.20 Seconds"
    if re.fullmatch(r"[\d\.\, ]+\s*-\s*[\d\.\, ]+.*", k):
        return False
    strong = (
        "winnaar","winning","top ","constructor",
        "kwalificatie","qualification","race","sprint",
        "championship","marge","margin","nationality",
        "classified","first ","any driver","number of","auto "
    )
    return any(w in k for w in strong)

# ---- Data classes
@dataclass
class Section: id:int; title:str
@dataclass
class Event: id:int; section_id:Optional[int]; name:str
@dataclass
class Market: id:int; event_id:Optional[int]; name:str
@dataclass
class Outcome: id:int; market_id:int; selection_name:str; odds_decimal:float; implied_prob:float; entity_id:Optional[int]
@dataclass
class Entity: id:int; type:Optional[str]; canonical_name:str; canonical_key:str

# ---- DB
SCHEMA = """
PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, fetched_at TEXT NOT NULL, url TEXT NOT NULL, html_sha256 TEXT NOT NULL, html TEXT
);
CREATE TABLE IF NOT EXISTS sections (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, section_id INTEGER NULL REFERENCES sections(id) ON DELETE SET NULL, name TEXT NOT NULL, UNIQUE(name));
CREATE TABLE IF NOT EXISTS markets (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NULL REFERENCES events(id) ON DELETE SET NULL, name TEXT NOT NULL, last_seen_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE, UNIQUE(name));
CREATE TABLE IF NOT EXISTS outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE, selection_name TEXT NOT NULL, odds_decimal REAL NOT NULL, implied_prob REAL NOT NULL, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS entities (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NULL, canonical_name TEXT NOT NULL, canonical_key TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS entity_aliases (id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, alias TEXT NOT NULL, alias_key TEXT NOT NULL, first_seen_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE, last_seen_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE, UNIQUE(entity_id, alias_key));
CREATE TABLE IF NOT EXISTS outcome_entities (outcome_id INTEGER PRIMARY KEY REFERENCES outcomes(id) ON DELETE CASCADE, entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE);
"""

class DB:
    def __init__(self, path="toto_f1.sqlite"):
        self.conn = sqlite3.connect(path); self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA); self.conn.commit()

    def new_snapshot(self, url, html)->int:
        sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
        cur = self.conn.execute("INSERT INTO snapshots (fetched_at,url,html_sha256,html) VALUES (?,?,?,?)",
                                (datetime.now(timezone.utc).replace(microsecond=0).isoformat(), url, sha, html))
        self.conn.commit(); return cur.lastrowid

    def upsert_section(self, title:str)->int:
        self.conn.execute("INSERT OR IGNORE INTO sections (title) VALUES (?)",(title,)); self.conn.commit()
        return self.conn.execute("SELECT id FROM sections WHERE title=?",(title,)).fetchone()["id"]

    def upsert_event(self, name, section_id)->int:
        self.conn.execute("INSERT OR IGNORE INTO events (name,section_id) VALUES (?,?)",(name, section_id))
        self.conn.commit()
        row = self.conn.execute("SELECT id,section_id FROM events WHERE name=?",(name,)).fetchone()
        if row and section_id and row["section_id"]!=section_id:
            self.conn.execute("UPDATE events SET section_id=? WHERE id=?",(section_id,row["id"])); self.conn.commit()
        return row["id"]

    def upsert_market(self, name, event_id, snapshot_id)->int:
        self.conn.execute("INSERT OR IGNORE INTO markets (name,event_id,last_seen_snapshot_id) VALUES (?,?,?)",
                          (name,event_id,snapshot_id)); self.conn.commit()
        row = self.conn.execute("SELECT id,event_id FROM markets WHERE name=?",(name,)).fetchone()
        self.conn.execute("UPDATE markets SET last_seen_snapshot_id=? WHERE id=?",(snapshot_id,row["id"])); self.conn.commit()
        if event_id and row["event_id"]!=event_id:
            self.conn.execute("UPDATE markets SET event_id=? WHERE id=?",(event_id,row["id"])); self.conn.commit()
        return row["id"]

    def insert_outcome(self, market_id, selection_name, odds_decimal, snapshot_id)->int:
        imp = implied_probability(odds_decimal)
        cur = self.conn.execute("INSERT INTO outcomes (market_id,selection_name,odds_decimal,implied_prob,snapshot_id) "
                                "VALUES (?,?,?,?,?)",(market_id, selection_name, odds_decimal, imp, snapshot_id))
        self.conn.commit(); return cur.lastrowid

    def upsert_entity_with_alias(self, name, snapshot_id, typ=None)->int:
        key = canonical_key(name)
        row = self.conn.execute("SELECT id FROM entities WHERE canonical_key=?",(key,)).fetchone()
        if row:
            entity_id = row["id"]; self._upsert_alias(entity_id, name, snapshot_id); return entity_id
        cur = self.conn.execute("INSERT INTO entities (type,canonical_name,canonical_key) VALUES (?,?,?)",(typ,name,key))
        self.conn.commit(); entity_id = cur.lastrowid; self._upsert_alias(entity_id, name, snapshot_id); return entity_id

    def _upsert_alias(self, entity_id, alias, snapshot_id):
        ak = canonical_key(alias)
        row = self.conn.execute("SELECT id FROM entity_aliases WHERE entity_id=? AND alias_key=?",(entity_id,ak)).fetchone()
        if row:
            self.conn.execute("UPDATE entity_aliases SET last_seen_snapshot_id=? WHERE id=?",(snapshot_id,row["id"]))
        else:
            self.conn.execute("INSERT INTO entity_aliases (entity_id,alias,alias_key,first_seen_snapshot_id,last_seen_snapshot_id) "
                              "VALUES (?,?,?,?,?)",(entity_id,alias,ak,snapshot_id,snapshot_id))
        self.conn.commit()

    def link_outcome_entity(self, outcome_id, entity_id):
        self.conn.execute("INSERT OR IGNORE INTO outcome_entities (outcome_id,entity_id) VALUES (?,?)",(outcome_id,entity_id))
        self.conn.commit()

    def list_outcomes_latest(self, market_id: int):
        snap = self.conn.execute(
            "SELECT MAX(snapshot_id) AS s FROM outcomes WHERE market_id=?", (market_id,)
        ).fetchone()["s"]
        rows = self.conn.execute(
            """SELECT o.id,o.market_id,o.selection_name,o.odds_decimal,o.implied_prob,oe.entity_id
            FROM outcomes o
            LEFT JOIN outcome_entities oe ON oe.outcome_id=o.id
            WHERE o.market_id=? AND o.snapshot_id=?
            ORDER BY o.id""",
            (market_id, snap)
        ).fetchall()
        return [Outcome(**dict(r)) for r in rows]

    # public API
    def list_sections(self)->List[Section]:
        return [Section(**dict(r)) for r in self.conn.execute("SELECT id,title FROM sections ORDER BY id")]
    def list_events(self, section_id:Optional[int]=None)->List[Event]:
        q="SELECT id,section_id,name FROM events"; args=()
        if section_id: q+=" WHERE section_id=?"; args=(section_id,)
        q+=" ORDER BY id"
        return [Event(**dict(r)) for r in self.conn.execute(q,args)]
    def list_markets(self, event_id:Optional[int]=None)->List[Market]:
        q="SELECT id,event_id,name FROM markets"; args=()
        if event_id: q+=" WHERE event_id=?"; args=(event_id,)
        q+=" ORDER BY id"
        return [Market(**dict(r)) for r in self.conn.execute(q,args)]
    def list_outcomes(self, market_id:int)->List[Outcome]:
        q = """SELECT o.id,o.market_id,o.selection_name,o.odds_decimal,o.implied_prob,oe.entity_id
               FROM outcomes o LEFT JOIN outcome_entities oe ON oe.outcome_id=o.id
               WHERE o.market_id=? ORDER BY o.id"""
        return [Outcome(**dict(r)) for r in self.conn.execute(q,(market_id,))]
    def find_entity(self, name_or_alias:str)->Optional[Entity]:
        k = canonical_key(name_or_alias)
        r = self.conn.execute("""SELECT e.id,e.type,e.canonical_name,e.canonical_key
                                 FROM entities e LEFT JOIN entity_aliases a ON a.entity_id=e.id
                                 WHERE e.canonical_key=? OR a.alias_key=? LIMIT 1""",(k,k)).fetchone()
        return Entity(**dict(r)) if r else None
    def entity_aliases(self, entity_id:int)->List[Tuple[str,int,int]]:
        return [(r["alias"],r["first_seen_snapshot_id"],r["last_seen_snapshot_id"])
                for r in self.conn.execute("SELECT alias,first_seen_snapshot_id,last_seen_snapshot_id FROM entity_aliases WHERE entity_id=?",(entity_id,))]
    def entity_odds_history(self, entity_id:int)->List[dict]:
        q = """SELECT o.id AS outcome_id,o.market_id,o.selection_name,o.odds_decimal,o.implied_prob,o.snapshot_id,m.name AS market_name
               FROM outcomes o JOIN outcome_entities oe ON oe.outcome_id=o.id JOIN markets m ON m.id=o.market_id
               WHERE oe.entity_id=? ORDER BY o.snapshot_id,o.id"""
        return [dict(r) for r in self.conn.execute(q,(entity_id,))]
    def close(self):
        self.conn.close()


# ---- Scraper
def guess_entity_type(n:str)->Optional[str]:
    l = n.lower()
    if "," in n: return "driver"
    if any(k in l for k in ["racing","amg","team","williams","mclaren","red bull","ferrari","mercedes","aston martin","sauber","alpine","rb"]):
        return "team"
    return None

class TotoF1Client:
    def __init__(self, db_path="toto_f1.sqlite", url=TOTO_F1_OUTRIGHTS_URL):
        self.db = DB(db_path); self.url = url

    def refresh(self, mode="auto", timeout=25, verify_tls=True) -> int:
        used_playwright = False
        html = None
        if mode in ("auto", "playwright"):
            try:
                html = self._fetch_rendered_html(timeout=timeout)
                used_playwright = True
            except Exception:
                if mode == "playwright":
                    raise
        if html is None:
            html = self._fetch_html(timeout=timeout, verify_tls=verify_tls)
            used_playwright = False

        snap_id = self.db.new_snapshot(self.url, html)
        lines = self._extract_text_lines(html, drop_noscript=used_playwright)
        sections = self._extract_sections(lines)
        sec_ids = {s:self.db.upsert_section(s) for s in sections}

        for market_title, items in self._extract_market_blocks(lines):
            event_id = None
            # naive event association: attach to section if suffix word appears
            maybe_section = self._match_section_for_market(market_title, sections)
            if maybe_section:
                event_id = self.db.upsert_event(maybe_section, sec_ids.get(maybe_section))
            m_id = self.db.upsert_market(market_title, event_id, snap_id)
            for sel, odd in items:
                if not ODDS_DUTCH_RE.match(odd): continue
                outcome_id = self.db.insert_outcome(m_id, sel, parse_dutch_decimal(odd), snap_id)
                ent_id = self.db.upsert_entity_with_alias(sel, snap_id, guess_entity_type(sel))
                self.db.link_outcome_entity(outcome_id, ent_id)
        return snap_id
    def close(self):
        self.db.close()

    # public API passthroughs
    def list_sections(self): return self.db.list_sections()
    def list_events(self, section_id:Optional[int]=None): return self.db.list_events(section_id)
    def list_markets(self, event_id:Optional[int]=None): return self.db.list_markets(event_id)
    def list_outcomes(self, market_id:int): return self.db.list_outcomes(market_id)
    def find_entity(self, name_or_alias:str): return self.db.find_entity(name_or_alias)
    def entity_aliases(self, entity_id:int): return self.db.entity_aliases(entity_id)
    def entity_odds_history(self, entity_id:int): return self.db.entity_odds_history(entity_id)

    # ---- internals
    def _fetch_html(self, timeout=25, verify_tls=True)->str:
        r = requests.get(self.url, headers={"User-Agent": USER_AGENT}, timeout=timeout, verify=verify_tls)
        r.raise_for_status(); return r.text

    def _fetch_rendered_html(self, timeout=30)->str:
        # Lazy import so requests-only users don't need Playwright
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=USER_AGENT, viewport={"width":1300,"height":1800})
            page = ctx.new_page()
            page.goto(self.url, wait_until="domcontentloaded", timeout=timeout*1000)
            # Try accept cookies if banner appears
            for sel in ["button:has-text('Akkoord')", "button:has-text('Accepteer')", "button:has-text('Alle cookies')"]:
                try: page.locator(sel).first.click(timeout=1500)
                except Exception: pass
            page.wait_for_load_state("networkidle", timeout=timeout*1000)

            # Click "Alles" tab if present (ensures combined view)
            try: page.get_by_role("link", name=re.compile(r"Alles", re.I)).first.click(timeout=1500)
            except Exception: pass

            # Expand ALL "Bekijk meer" buttons
            # Re-query until none remain or iteration cap reached
            for _ in range(12):
                buttons = page.locator("text=Bekijk meer")
                count = buttons.count()
                if count == 0: break
                for i in range(count):
                    try: buttons.nth(i).click(timeout=1500)
                    except Exception: pass
                page.wait_for_timeout(600)  # let content append
            html = page.content()
            ctx.close(); browser.close()
            return html

    def _extract_text_lines(self, html: str, drop_noscript: bool = False):
        soup = BeautifulSoup(html, "lxml")
        # Always drop script/style; drop noscript only when content came from Playwright
        for tag in soup(["script", "style"] + (["noscript"] if drop_noscript else [])):
            tag.decompose()
        text = soup.get_text("\n")
        return [wsnorm(x) for x in text.splitlines() if wsnorm(x)]


    def _extract_sections(self, lines):
        tabs, capture = [], False
        for s in lines:
            sl = s.strip()
            if sl.lower() == "alles":
                capture = True
                continue
            if not capture:
                continue
            low = sl.lower()
            if " - " in sl:  # markets have hyphens; tabs shouldn’t
                if len(tabs) >= 3:
                    break
                else:
                    continue
            if re.search(r"(grand prix|formula|formule|qualification|kwalificatie|sprint|race|20\d{2})", low):
                if sl not in tabs:
                    tabs.append(sl)
            elif len(tabs) >= 3:
                break
        return tabs

    def _extract_market_blocks(self, lines):
        blocks, title, items = [], None, []
        def flush():
            nonlocal title, items
            if title and items:
                # de-dupe selections within a single market block (name+odds)
                seen = set(); uniq = []
                for sel, odd in items:
                    key = (sel, odd)
                    if key not in seen:
                        seen.add(key); uniq.append((sel, odd))
                blocks.append((title, uniq))
            title, items = None, []

        i = 0
        while i < len(lines):
            ln = wsnorm(lines[i])

            if title is None and looks_like_market_title(ln):
                title = ln; i += 1; continue

            if title is not None:
                if ln.lower().startswith("bekijk meer"):
                    i += 1; continue
                # start a new market only on a strong title
                if looks_like_market_title(ln):
                    flush(); title = ln; i += 1; continue
                if i + 1 < len(lines) and ODDS_DUTCH_RE.match(lines[i+1]):
                    items.append((ln, lines[i+1])); i += 2; continue

            i += 1
        flush(); return blocks

    def _match_section_for_market(self, market_title: str, sections: List[str])->Optional[str]:
        mt = market_title.lower()
        for s in sections:
            if any(k in mt for k in ["race","kwalificatie","qualifying","sprint","shootout"]) and \
               any(k in s.lower() for k in ["race","qualification","kwalificatie","sprint"]):
                return s
        # Fallback: season markets → Formula 1 YYYY if present
        for s in sections:
            if re.search(r"(formula|formule)\s*1\s*20\d{2}", s.lower()): return s
        return None

# ---- CLI
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="toto_f1.sqlite")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--mode", default="auto", choices=["auto","playwright","requests"])
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    cli = TotoF1Client(db_path=args.db)

    # 1) Do explicit refresh if requested
    if args.refresh:
        snap = cli.refresh(mode=args.mode)
        print(f"Snapshot stored: {snap}")

    # 2) If listing and DB is empty *and you did not request refresh*, do one auto refresh
    if args.list and not args.refresh and not cli.db.list_markets():
        print("DB empty -> refreshing once (requests mode) ...")
        cli.refresh(mode="requests")

    if args.list:
        print("== Sections ==")
        for s in cli.list_sections():
            print(f"[{s.id}] {s.title}")
        print("\n== Markets & sample outcomes ==")
        for m in cli.list_markets():
            print(f"[{m.id}] {m.name}")
            outs = cli.list_outcomes(m.id)  # or list_outcomes_latest(...) if you add it
            for o in outs[:10]:
                print(f"   - {o.selection_name}: {o.odds_decimal:.2f} (p={o.implied_prob:.3f})")
            if len(outs) > 10:
                print(f"   ... {len(outs)-10} more")
