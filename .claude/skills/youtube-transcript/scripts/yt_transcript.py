#!/usr/bin/env python3
"""YouTube-Transkript holen, bereinigen, speichern und in Chunks aufteilen.

Aufruf:
    yt_transcript.py URL [--lang de,en] [--out DIR] [--chunk-minutes 20] [--plain] [--list]
                      [--backend auto|ytdlp|supadata] [--fix|--no-fix]
    yt_transcript.py URL --from-text FILE|- [--append] [--title T] [--channel C] [--duration H:MM:SS]
                      [--track-kind auto|manual] [--track-lang de]

Import-Modus (--from-text): liest Text aus dem YouTube-Panel "Transkript anzeigen" (Datei oder
stdin mit "-"), ohne jeden Netzwerkzugriff. Der Rohtext liegt danach in <out>/<ID>/raw_panel.txt;
mit --append wird weiterer Panel-Text ergaenzt (Ueberlappung wird per (Zeit, Text) entdoppelt).

Korrekturen (corrections.tsv neben scripts/, plus ~/.config/yt-transcript/corrections.tsv):
ganze Woerter, Gross-/Kleinschreibung beachtet. Standard: nur bei automatischen Spuren.

Ausgabe (Standard: ./yt-transcripts/<VIDEO_ID>/):
    transcript.md   Kopf mit Metadaten + Kapitel + kompletter Text
    chunks/*.md     Abschnitte (nach Kapiteln, sonst nach --chunk-minutes)
    meta.json       Metadaten, gewaehlte Spur, Chunk-Index

Auf stdout erscheint nur eine kompakte Zusammenfassung (nie das Transkript).

Exit-Codes:
    0 OK | 1 unerwarteter Fehler | 2 ungueltige URL/ID | 3 Video-Metadaten nicht abrufbar
    4 keine Untertitel verfuegbar | 5 Untertitel-Download fehlgeschlagen (z. B. HTTP 429)

Backends (--backend): "ytdlp" (Standard ohne Key) oder "supadata" (bezahlte Transkript-API,
nur vorhandene Untertitel, mode=native). "auto" nimmt Supadata, wenn SUPADATA_API_KEY gesetzt
ist, sonst yt-dlp. Der Key wird nur aus der Umgebung gelesen, nie ausgegeben.

Spurauswahl (siehe choose_track, nur yt-dlp): Handgemachte Untertitel (manuell) werden vor
automatischen bevorzugt. Reihenfolge:
    1. manuell de*      2. manuell en*
    3. auto de* (Original) 4. auto en* (Original)
    5. manuell in Originalsprache   6. auto in Originalsprache (Key "*-orig")
    7. irgendeine manuelle Spur     8. irgendeine automatische Originalspur
    9. maschinell uebersetzte Spur (letzter Ausweg)
Die Reihenfolge von 1-4 richtet sich nach --lang (Standard "de,en").
Maschinell uebersetzte Auto-Spuren erkennt man an "tlang=" in der URL; sie werden
nur im letzten Schritt genutzt.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse

BACKOFF = (5, 15, 40)
PARA_SECONDS = 30
TOKENS_PER_WORD = 1.4
SUPADATA_BASE = "https://api.supadata.ai/v1"
SUPADATA_POLL_SECONDS = 3
SUPADATA_POLL_MAX = 300


# --------------------------------------------------------------------------- util
def die(code, msg):
    print(f"FEHLER: {msg}", file=sys.stderr)
    sys.exit(code)


def ensure_yt_dlp():
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        print("yt-dlp fehlt, installiere es ...", file=sys.stderr)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "yt-dlp"])
        if r.returncode != 0:
            die(1, "yt-dlp konnte nicht installiert werden (pip install yt-dlp).")


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def hms_compact(sec):
    return hms(sec).replace(":", "")


def parse_video_id(s):
    """Extrahiert die 11-stellige Video-ID aus URL oder gibt eine nackte ID zurueck."""
    s = s.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    if "://" not in s:
        s = "https://" + s
    u = urlparse(s)
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    cand = None
    if host.endswith("youtu.be") and parts:
        cand = parts[0]
    elif "youtube" in host or "youtube-nocookie" in host:
        if parts and parts[0] in ("shorts", "live", "embed", "v", "e") and len(parts) > 1:
            cand = parts[1]
        else:
            cand = (parse_qs(u.query).get("v") or [None])[0]
    if cand and re.fullmatch(r"[A-Za-z0-9_-]{11}", cand):
        return cand
    return None


def retry(fn, what):
    """Fuehrt fn aus; bei HTTP 429 Wiederholung mit Backoff (5/15/40 s)."""
    for i in range(len(BACKOFF) + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if "429" in str(e) or "Too Many Requests" in str(e):
                if i < len(BACKOFF):
                    print(f"HTTP 429 bei {what}, warte {BACKOFF[i]} s ...", file=sys.stderr)
                    time.sleep(BACKOFF[i])
                    continue
            raise


# ----------------------------------------------------------------------- metadata
def fetch_info(video_id, cookies=None):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = retry(lambda: ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False), "Metadaten")
        return ydl, info


def is_translated(entries):
    return any("tlang=" in (e.get("url") or "") for e in entries)


def lang_match(key, prefixes):
    base = key.split("-")[0].lower()
    return any(base == p for p in prefixes)


def choose_track(info, langs):
    """Gibt (key, is_auto, entries) oder None zurueck."""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    manual = {k: v for k, v in manual.items() if k != "live_chat" and v}
    auto_orig = {k: v for k, v in auto.items() if v and not is_translated(v)}
    auto_trans = {k: v for k, v in auto.items() if v and is_translated(v)}
    orig = (info.get("language") or "").split("-")[0].lower()

    def first(d, pred):
        # "-orig" und exakte Treffer zuerst, Regionalvarianten danach
        for k in sorted(d, key=lambda k: (not k.endswith("-orig"), len(k), k)):
            if pred(k):
                return k
        return None

    cands = []
    for lg in langs:
        cands.append((manual, False, lambda k, lg=lg: lang_match(k, [lg])))
    for lg in langs:
        cands.append((auto_orig, True, lambda k, lg=lg: lang_match(k, [lg])))
    if orig:
        cands.append((manual, False, lambda k: lang_match(k, [orig])))
        cands.append((auto_orig, True, lambda k: lang_match(k, [orig])))
    cands.append((manual, False, lambda k: True))
    cands.append((auto_orig, True, lambda k: True))
    cands.append((auto_trans, True, lambda k: lang_match(k, langs)))
    cands.append((auto_trans, True, lambda k: True))
    for d, is_auto, pred in cands:
        k = first(d, pred)
        if k:
            return k, is_auto, d[k]
    return None


def list_tracks(info):
    print("Manuelle Untertitel:", ", ".join(sorted((info.get("subtitles") or {}))) or "keine")
    auto = info.get("automatic_captions") or {}
    orig = [k for k, v in auto.items() if v and not is_translated(v)]
    trans = [k for k, v in auto.items() if v and is_translated(v)]
    print("Automatisch (Original):", ", ".join(sorted(orig)) or "keine")
    print(f"Automatisch uebersetzt: {len(trans)} Sprachen (werden nur als letzter Ausweg genutzt)")


# --------------------------------------------------------------------- download
def pick_entry(entries):
    for ext in ("json3", "vtt"):
        for e in entries:
            if e.get("ext") == ext and e.get("url"):
                return ext, e["url"]
    return None, None


def download(ydl, url):
    def go():
        resp = ydl.urlopen(url)
        return resp.read().decode("utf-8", errors="replace")
    return retry(go, "Untertitel-Download")


# ---------------------------------------------------------------------- parsing
def parse_json3(text):
    data = json.loads(text)
    out = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        t = "".join(s.get("utf8", "") for s in segs)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            out.append((ev["tStartMs"] / 1000.0, t))
    return out


_TS = re.compile(r"(?:(\d+):)?(\d+):(\d+)[.,](\d+)")


def parse_vtt(text):
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = block.strip().split("\n")
        for i, ln in enumerate(lines):
            if "-->" in ln:
                m = _TS.match(ln.split("-->")[0].strip())
                if not m:
                    break
                h, mi, s, ms = m.groups()
                start = int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000
                t = " ".join(lines[i + 1:])
                t = re.sub(r"<[^>]+>", "", t)
                t = re.sub(r"\s+", " ", t).strip()
                if t:
                    out.append((start, t))
                break
    return out


def clean_segments(segs, rolling=True):
    """Entfernt Rolling-/Doppel-Fragmente (typisch bei Auto-Untertiteln).

    rolling=False (Panel-Import): nur Muell entfernen, Text nie verschmelzen/kuerzen."""
    out = []  # [start, text]
    for start, text in segs:
        text = text.replace("​", "").strip()
        text = re.sub(r"\[(?:Music|Musik|Applause|Applaus)\]", "", text, flags=re.I).strip()
        if not text:
            continue
        if not rolling:
            out.append([start, text])
            continue
        if not out:
            out.append([start, text])
            continue
        prev = out[-1][1]
        if text == prev or prev.endswith(text):
            continue
        if text.startswith(prev):  # Rolling: neuer Text erweitert alten
            out[-1][1] = text
            continue
        # Wort-Ueberlappung Ende(prev) / Anfang(text) entfernen
        pw, tw = prev.split(), text.split()
        k = min(len(pw), len(tw), 12)
        while k > 0 and pw[-k:] != tw[:k]:
            k -= 1
        rest = " ".join(tw[k:]) if k else text
        if rest:
            out.append([start, rest])
    return out


def build_paragraphs(segs):
    """Gruppiert bereinigte Segmente zu Absaetzen von ca. 30 s -> [(start, text)]."""
    paras, cur, cur_start = [], [], None
    for start, text in segs:
        if cur_start is None:
            cur_start = start
        cur.append(text)
        dur = start - cur_start
        ends_sentence = text[-1:] in ".!?…"
        if (dur >= PARA_SECONDS and ends_sentence) or dur >= PARA_SECONDS * 1.6:
            paras.append((cur_start, " ".join(cur)))
            cur, cur_start = [], None
    if cur:
        paras.append((cur_start, " ".join(cur)))
    return paras


def fallback_api(video_id, langs):
    """Letzter Ausweg: youtube-transcript-api."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None, None
    try:
        try:
            tr = YouTubeTranscriptApi().fetch(video_id, languages=list(langs) + ["en"])
            segs = [(s.start, s.text) for s in tr]
        except AttributeError:
            tr = YouTubeTranscriptApi.get_transcript(video_id, languages=list(langs) + ["en"])
            segs = [(s["start"], s["text"]) for s in tr]
        return segs, "youtube-transcript-api"
    except Exception as e:  # noqa: BLE001
        print(f"youtube-transcript-api fehlgeschlagen: {str(e).splitlines()[0][:160]}", file=sys.stderr)
        return None, None


# --------------------------------------------------------------------- supadata
class SupadataError(Exception):
    """kind: auth | quota | none | other"""

    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind


def sd_http(path, params, key):
    """GET gegen die Supadata-API -> (status, json). Wirft SupadataError bei Fehlern."""
    url = f"{SUPADATA_BASE}{path}" + (f"?{urlencode(params)}" if params else "")
    req = urllib.request.Request(url, headers={
        "x-api-key": key, "Accept": "application/json", "User-Agent": "yt-transcript-skill/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, body = r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        raise SupadataError("other", f"Supadata nicht erreichbar: {str(e)[:160]}")
    try:
        data = json.loads(body) if body.strip() else {}
    except ValueError:
        data = {}
    if status in (401, 403):
        raise SupadataError("auth", "Supadata-API-Key ungueltig (SUPADATA_API_KEY pruefen).")
    if status in (402, 429):
        raise SupadataError("quota", "Supadata-Kontingent bzw. Rate-Limit erreicht.")
    if status in (206, 404):
        raise SupadataError("none", "Keine Untertitel fuer dieses Video verfuegbar.")
    if status >= 400:
        detail = (data.get("message") or data.get("error") or "") if isinstance(data, dict) else ""
        raise SupadataError("other", f"Supadata-Fehler HTTP {status}: {str(detail)[:160]}")
    return status, data


def sd_content(data):
    """Findet die Chunk-Liste, auch wenn sie unter 'result' verschachtelt ist."""
    if not isinstance(data, dict):
        return None, None, []
    if isinstance(data.get("content"), list):
        return data["content"], data.get("lang"), data.get("availableLangs") or []
    if isinstance(data.get("result"), dict):
        return sd_content(data["result"])
    return None, None, []


def sd_transcript_call(vid, lang, key):
    """Ein Transkript-Aufruf (mode=native); pollt bei 202 den Job. -> (content, lang, available)."""
    params = {"url": f"https://www.youtube.com/watch?v={vid}", "mode": "native"}
    if lang:
        params["lang"] = lang
    status, data = sd_http("/transcript", params, key)
    if status == 202 or (isinstance(data, dict) and data.get("jobId") and "content" not in data):
        job = data.get("jobId")
        waited = 0
        while waited < SUPADATA_POLL_MAX:
            time.sleep(SUPADATA_POLL_SECONDS)
            waited += SUPADATA_POLL_SECONDS
            _, data = sd_http(f"/transcript/{job}", None, key)
            st = (data.get("status") or "").lower() if isinstance(data, dict) else ""
            if st == "failed":
                err = data.get("error")
                msg = err.get("message") if isinstance(err, dict) else err
                raise SupadataError("other", f"Supadata-Job fehlgeschlagen: {str(msg or '')[:160]}")
            if st == "completed" or sd_content(data)[0] is not None:
                break
        else:
            raise SupadataError("other", "Supadata-Job nicht rechtzeitig fertig (Timeout nach 5 Minuten).")
    content, got, avail = sd_content(data)
    if not content:
        raise SupadataError("none", "Keine Untertitel fuer dieses Video verfuegbar.")
    return content, got, avail


def sd_segments(content):
    """Supadata-Chunks (offset in ms) -> [(start_sekunden, text)]."""
    out = []
    for c in content:
        t = re.sub(r"\s+", " ", str(c.get("text") or "")).strip()
        if t:
            out.append(((c.get("offset") or 0) / 1000.0, t))
    return out


def sd_transcript(vid, langs, key):
    """Max. 2 Aufrufe: erst langs[0]; weicht die Sprache ab und ist langs[1:] verfuegbar, noch einmal."""
    content, got, avail = sd_transcript_call(vid, langs[0], key)
    if got and got.lower() != langs[0]:
        avail_l = [x.lower() for x in avail]
        nxt = next((l for l in langs[1:] if l != got.lower() and l in avail_l), None)
        if nxt:
            content, got, avail = sd_transcript_call(vid, nxt, key)
    return sd_segments(content), (got or langs[0])


_CH = re.compile(r"^\s*(?:[-*\u2022]\s*)?\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?\s*[-\u2013\u2014:|]?\s*(.+?)\s*$")


def parse_chapters(desc):
    """Kapitel aus Beschreibungszeilen wie '0:00 Intro' (wie YouTube: erstes bei 0:00, >= 3)."""
    found = []
    for ln in (desc or "").splitlines():
        m = _CH.match(ln)
        if not m:
            continue
        parts = [int(x) for x in m.group(1).split(":")]
        sec = parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]
        if found and sec <= found[-1]["start_time"]:
            continue
        found.append({"start_time": sec, "title": m.group(2)})
    if len(found) < 3 or found[0]["start_time"] != 0:
        return []
    for c, n in zip(found, found[1:]):
        c["end_time"] = n["start_time"]
    return found


def sd_info(vid, key):
    """Metadaten ueber /youtube/video im yt-dlp-aehnlichen Dict-Format (1 Aufruf)."""
    _, d = sd_http("/youtube/video", {"id": vid}, key)
    d = d if isinstance(d, dict) else {}
    ch = d.get("channel")
    ud = (d.get("uploadDate") or "")[:10]
    desc = d.get("description") or ""
    return {"title": d.get("title"), "channel": ch.get("name") if isinstance(ch, dict) else ch,
            "upload_date": ud.replace("-", ""), "duration": int(d.get("duration") or 0),
            "description": desc, "chapters": parse_chapters(desc),
            "transcript_languages": d.get("transcriptLanguages") or []}


# ------------------------------------------------------------------ panel import
PANEL_SEP = "-----"
_UNIT = r"(?:Stunden?|Std\.?|Minuten?|Min\.?|Sekunden?|Sek\.?|hours?|hrs?|minutes?|mins?|seconds?|secs?)"
_ACC = re.compile(rf"^\d+\s*{_UNIT}(?:[\s,]+(?:und\s+|and\s+)?\d+\s*{_UNIT})*$", re.I)
_TS_ONLY = re.compile(r"^((?:\d{1,2}:)?\d{1,2}:\d{2})$")
_TS_TEXT = re.compile(r"^((?:\d{1,2}:)?\d{1,2}:\d{2})\s+(\S.*)$")
_AUTO_LANG = re.compile(
    r"^(\S[^()]*?)\s*\((?:automatisch(?: erzeugt)?|auto-generated|automatically generated)\)$", re.I)
_LANGS = {"deutsch": "de", "englisch": "en", "english": "en", "german": "de",
          "franzoesisch": "fr", "französisch": "fr", "french": "fr", "spanisch": "es",
          "spanish": "es", "italienisch": "it", "italian": "it"}
_NOISE = {"transkript", "transkript anzeigen", "transkript ausblenden", "kapitel", "untertitel",
          "im video suchen", "transcript", "show transcript", "chapters", "search in video",
          "...mehr", "mehr", "weniger anzeigen", "nach kapiteln"}


def _secs(ts):
    p = [int(x) for x in ts.split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]


def _acc_secs(line):
    total = 0
    for n, unit in re.findall(r"(\d+)\s*([A-Za-z]+)", line):
        u = unit.lower()
        total += int(n) * (3600 if u.startswith(("stunde", "std", "h")) else 60 if u.startswith("m") else 1)
    return total


def parse_duration(s):
    """'1:23:45', '83:20' oder Sekunden -> int Sekunden (None bei Unsinn)."""
    s = (s or "").strip()
    if re.fullmatch(r"\d+", s):
        return int(s)
    return _secs(s) if _TS_ONLY.match(s) else None


def _header_like(line):
    return (len(line) <= 80 and len(line.split()) <= 12 and not line[-1] in ".!?…,;:"
            and not line[0].islower())


def parse_panel(text):
    """Text des Panels 'Transkript anzeigen' -> dict(segs, chapters, lang, auto, dupes).

    segs: [(sekunden, text)] sortiert, entdoppelt nach (Zeit, Text)."""
    toks, lang, auto, seen_ts = [], None, False, False
    for raw in text.replace("\r", "").replace("​", "").split("\n"):
        ln = re.sub(r"\s+", " ", raw).strip()
        if not ln:
            continue
        low = ln.lower()
        if re.fullmatch(r"-{5,}", ln):
            toks.append(("sep",))
            continue
        if low in _NOISE:
            continue
        m = _AUTO_LANG.match(ln)
        if m:
            auto = True
            lang = _LANGS.get(m.group(1).strip().lower()) or lang
            continue
        if not seen_ts and low in _LANGS:
            lang = _LANGS[low]
            continue
        m = _TS_ONLY.match(ln) or _TS_TEXT.match(ln)
        if m:
            seen_ts = True
            toks.append(("ts", _secs(m.group(1)), m.group(2) if m.lastindex == 2 else ""))
        elif _ACC.match(ln):
            toks.append(("acc", _acc_secs(ln), ln))
        else:
            toks.append(("text", ln))
    # Barrierefreiheits-Zeilen: mit numerischen Zeitstempeln ueberfluessig (neben einem ts -> weg,
    # sonst normaler Text); ohne numerische Zeitstempel dienen sie selbst als Zeitstempel.
    has_num = any(t[0] == "ts" for t in toks)
    fixed = []
    for i, t in enumerate(toks):
        if t[0] != "acc":
            fixed.append(t)
        elif not has_num:
            fixed.append(("ts", t[1], ""))
        elif not ((i > 0 and toks[i - 1][0] == "ts") or (i + 1 < len(toks) and toks[i + 1][0] == "ts")):
            fixed.append(("text", t[2]))
    toks = fixed

    entries, chapters, cur, pend = [], [], None, None
    for i, t in enumerate(toks):
        if t[0] == "sep":
            cur, pend = None, None
        elif t[0] == "ts":
            cur = [t[1], t[2]]
            entries.append(cur)
            if pend:
                chapters.append((t[1], pend))
                pend = None
        elif cur is None:  # Text vor dem ersten Zeitstempel: Kapitelkopf oder Rauschen
            if _header_like(t[1]):
                pend = t[1]
        elif not cur[1]:
            cur[1] = t[1]
        elif _header_like(t[1]) and i + 1 < len(toks) and toks[i + 1][0] == "ts":
            pend = t[1]  # Kapitelueberschrift zwischen zwei Eintraegen
        else:
            cur[1] += " " + t[1]

    seen, segs, dupes = set(), [], 0
    for ts, tx in entries:
        if not tx:
            continue
        if (ts, tx) in seen:
            dupes += 1
            continue
        seen.add((ts, tx))
        segs.append((float(ts), tx))
    segs.sort(key=lambda s: s[0])
    chs = {}
    for ts, title in chapters:
        chs.setdefault(ts, title)
    chs = sorted(chs.items())
    if len(chs) < 2:
        chs = []
    elif chs[0][0] > 0:
        chs.insert(0, (0, "Anfang"))
    chapters = [{"start_time": s, "title": ti} for s, ti in chs]
    for c, n in zip(chapters, chapters[1:]):
        c["end_time"] = n["start_time"]
    return {"segs": segs, "chapters": chapters, "lang": lang, "auto": auto, "dupes": dupes}


# ------------------------------------------------------------------ corrections
def load_corrections():
    """corrections.tsv (Skill-Ordner) + ~/.config/yt-transcript/corrections.tsv (ueberschreibt) -> (dict, [Pfade])."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    table, files = {}, []
    for p in (os.path.join(root, "corrections.tsv"),
              os.path.expanduser("~/.config/yt-transcript/corrections.tsv")):
        if not os.path.isfile(p):
            continue
        files.append(p)
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.rstrip("\r\n")
                if not ln.strip() or ln.lstrip().startswith("#"):
                    continue
                parts = ln.split("\t")
                if len(parts) < 2:
                    continue
                k, v = " ".join(parts[0].split()), parts[1].strip()
                if k and v and k != v:
                    table[k] = v
    return table, files


def apply_corrections(segs, table):
    """Ganze Woerter, Gross-/Kleinschreibung beachtet. -> (segs, {(falsch, richtig): anzahl})."""
    counts = {}
    if not table:
        return segs, counts
    keys = sorted(table, key=len, reverse=True)
    pat = re.compile(r"(?<!\w)(?:" + "|".join(r"\s+".join(re.escape(w) for w in k.split()) for k in keys)
                     + r")(?!\w)")

    def sub(m):
        k = " ".join(m.group(0).split())
        counts[(k, table[k])] = counts.get((k, table[k]), 0) + 1
        return table[k]

    return [[s, pat.sub(sub, t)] for s, t in segs], counts


# --------------------------------------------------------------------- chunking
def make_ranges(chapters, duration, chunk_min):
    """Liefert [(start, end, titel)] gemaess Kapiteln oder festen Fenstern."""
    size = chunk_min * 60
    if not chapters:
        return [(s, min(s + size, duration), "") for s in range(0, int(duration), size)]
    chs = [(c["start_time"], c.get("end_time") or duration, c.get("title", "")) for c in chapters]
    # tiny chapters mergen (< 1/3 Chunkgroesse), solange Gesamtgroesse <= 1.25 * Chunkgroesse
    merged = []
    for s, e, t in chs:
        if merged and (merged[-1][1] - merged[-1][0] < size / 3) and (e - merged[-1][0] <= size * 1.25):
            ps, _, pt, n = merged[-1]
            merged[-1] = (ps, e, pt, n + 1)
        else:
            merged.append((s, e, t, 1))
    out = []
    for s, e, t, n in merged:
        title = t if n == 1 else f"{t} (+{n - 1} weitere Kapitel)"
        length = e - s
        if length > size * 1.25:  # lange Kapitel gleichmaessig teilen
            parts = int(-(-length // size))
            step = length / parts
            for i in range(parts):
                out.append((s + i * step, s + (i + 1) * step, f"{title} [Teil {i + 1}/{parts}]"))
        else:
            out.append((s, e, title))
    return out


def est_tokens(words):
    return int(words * TOKENS_PER_WORD)


def fmt_par(start, text, plain):
    return text if plain else f"[{hms(start)}] {text}"


# ------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="YouTube-Transkript holen und in Chunks speichern.")
    ap.add_argument("url", help="YouTube-URL (watch, youtu.be, shorts, live, embed) oder 11-stellige ID")
    ap.add_argument("--lang", default="de,en", help="Bevorzugte Sprachen, kommagetrennt (Standard: de,en)")
    ap.add_argument("--out", default="yt-transcripts", help="Ausgabeordner (Standard: ./yt-transcripts)")
    ap.add_argument("--chunk-minutes", type=int, default=20, help="Zielgroesse eines Chunks in Minuten (20)")
    ap.add_argument("--plain", action="store_true", help="Ohne Zeitstempel ausgeben")
    ap.add_argument("--cookies", help="cookies.txt (Netscape-Format) fuer Altersfreigabe/Bot-Pruefung")
    ap.add_argument("--list", action="store_true", help="Nur verfuegbare Untertitelspuren anzeigen")
    ap.add_argument("--backend", choices=("auto", "ytdlp", "supadata"), default="auto",
                    help="Quelle: auto (Supadata, wenn SUPADATA_API_KEY gesetzt, sonst yt-dlp), ytdlp, supadata")
    ap.add_argument("--from-text", metavar="FILE",
                    help="Import-Modus: Text aus dem Panel 'Transkript anzeigen' (Datei oder '-' fuer stdin), ohne Netzwerk")
    ap.add_argument("--append", action="store_true",
                    help="Mit --from-text: Text an vorhandenes raw_panel.txt anhaengen (mehrstufiges Einlesen)")
    ap.add_argument("--title", help="Titel (nur --from-text)")
    ap.add_argument("--channel", help="Kanal (nur --from-text)")
    ap.add_argument("--duration", help="Videodauer H:MM:SS, MM:SS oder Sekunden (nur --from-text)")
    ap.add_argument("--track-kind", choices=("auto", "manual"),
                    help="Spurart im Import-Modus (Standard: auto, bzw. aus dem Panel-Kopf erkannt)")
    ap.add_argument("--track-lang", help="Sprache im Import-Modus (Standard: de, bzw. aus dem Panel-Kopf)")
    ap.add_argument("--fix", action="store_true", help="corrections.tsv immer anwenden (Standard: nur bei automatischen Spuren)")
    ap.add_argument("--no-fix", action="store_true", help="corrections.tsv nie anwenden")
    a = ap.parse_args()
    if a.fix and a.no_fix:
        die(2, "--fix und --no-fix schliessen sich aus.")
    if a.append and not a.from_text:
        die(2, "--append gibt es nur zusammen mit --from-text.")

    vid = parse_video_id(a.url)
    if not vid:
        die(2, "Keine gueltige YouTube-URL oder Video-ID erkannt.")
    langs = [l.strip().lower() for l in a.lang.split(",") if l.strip()] or ["de", "en"]

    key_env = os.environ.get("SUPADATA_API_KEY", "").strip()
    backend = a.backend
    if a.from_text:
        backend = "text"
    elif backend == "auto":
        backend = "supadata" if key_env else "ytdlp"
    if backend == "supadata" and not key_env:
        die(3, "SUPADATA_API_KEY ist nicht gesetzt. Key als Umgebungsvariable (Netzwerk-Secret) "
               "in den Umgebungseinstellungen hinterlegen - nie im Chat einfuegen.")

    if backend == "text":
        try:
            if a.from_text == "-":
                raw_new = sys.stdin.read()
            else:
                with open(a.from_text, encoding="utf-8", errors="replace") as f:
                    raw_new = f.read()
        except OSError as e:
            die(2, f"Textdatei nicht lesbar: {e}")
        base = os.path.join(a.out, vid)
        os.makedirs(base, exist_ok=True)
        raw_path = os.path.join(base, "raw_panel.txt")
        raw = raw_new
        if os.path.isfile(raw_path):
            if a.append:
                with open(raw_path, encoding="utf-8") as f:
                    raw = f.read().rstrip("\n") + f"\n{PANEL_SEP}\n" + raw_new
            else:
                print("Hinweis: vorhandenes raw_panel.txt wird ersetzt (--append zum Ergaenzen).", file=sys.stderr)
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(raw.rstrip("\n") + "\n")
        panel = parse_panel(raw)
        segs, chapters = panel["segs"], panel["chapters"]
        if not segs:
            die(4, "Im Text wurden keine Zeitstempel-Eintraege erkannt (Format z. B. '0:00' + Text-Zeile).")
        track_kind = a.track_kind or "auto"
        key = (a.track_lang or panel["lang"] or "de").lower()
        is_auto, fmt, source = track_kind == "auto", "text", "Browser-Import (Transkript-Panel)"
        duration = parse_duration(a.duration) if a.duration else 0
        if a.duration and duration is None:
            die(2, "--duration nicht lesbar (erwartet H:MM:SS, MM:SS oder Sekunden).")
        info = {"title": a.title or "unbekannt", "channel": a.channel or "unbekannt",
                "upload_date": "unbekannt", "language": key, "description": ""}
    elif backend == "supadata":
        try:
            info = sd_info(vid, key_env)
        except SupadataError as e:
            die(3, f"Metadaten fuer {vid} nicht abrufbar: {e}")
        if a.list:
            print(f"{info.get('title')}  [{vid}]  Backend: Supadata (native)")
            print("Verfuegbare Untertitelsprachen:", ", ".join(info["transcript_languages"]) or "keine")
            return
        duration = info.get("duration") or 0
        chapters = info.get("chapters") or []
        try:
            segs, key = sd_transcript(vid, langs, key_env)
        except SupadataError as e:
            die(4 if e.kind == "none" else 5, str(e))
        source, is_auto, fmt = "Supadata (native)", False, "api"
        info["language"] = key
    else:
        ensure_yt_dlp()
        try:
            ydl, info = fetch_info(vid, a.cookies)
        except Exception as e:  # noqa: BLE001
            msg = str(e).replace("ERROR: ", "").splitlines()[0][:300]
            hint = ""
            low = msg.lower()
            if "not a bot" in low:
                hint = (" (YouTube-Bot-Pruefung/IP-Drosselung: einige Minuten warten, evtl. --cookies cookies.txt angeben."
                        " Alternative: SUPADATA_API_KEY in den Umgebungseinstellungen (Netzwerk-Secrets/Umgebungsvariable) "
                        "setzen, dann nutzt --backend auto die Supadata-API. Oder Transkript-Panel-Text per --from-text einlesen)")
            elif "age" in low or "sign in" in low:
                hint = " (Altersbeschraenkt/Anmeldung noetig: --cookies cookies.txt angeben oder Transkript manuell einfuegen)"
            elif "429" in msg:
                hint = " (Rate-Limit; spaeter erneut versuchen)"
            elif "private" in low or "unavailable" in low:
                hint = " (Video privat oder nicht verfuegbar)"
            die(3, f"Metadaten fuer {vid} nicht abrufbar: {msg}{hint}")

        if a.list:
            print(f"{info.get('title')}  [{vid}]  Sprache: {info.get('language')}")
            list_tracks(info)
            return

        duration = info.get("duration") or 0
        chapters = info.get("chapters") or []
        track = choose_track(info, langs)
        segs, source, key, is_auto, fmt = None, None, None, None, None
        if track:
            key, is_auto, entries = track
            fmt, url = pick_entry(entries)
            if url:
                try:
                    raw = download(ydl, url)
                    segs = parse_json3(raw) if fmt == "json3" else parse_vtt(raw)
                    source = "yt-dlp"
                except Exception as e:  # noqa: BLE001
                    print(f"Download der Spur '{key}' fehlgeschlagen: {str(e).splitlines()[0][:160]}", file=sys.stderr)
        if not segs:
            segs, source = fallback_api(vid, langs)
            if segs:
                key, is_auto, fmt = "api", True, "api"
        if not segs:
            if track:
                die(5, "Untertitel-Download fehlgeschlagen (vermutlich HTTP 429 / IP-Sperre). "
                       "Spaeter erneut versuchen oder anderes Netz nutzen.")
            die(4, "Keine Untertitel fuer dieses Video verfuegbar. "
                   "Transkript-Panel-Text per --from-text einlesen (YouTube: '...' > 'Transkript anzeigen').")

    clean = clean_segments(segs, rolling=backend != "text")
    apply_fix = True if a.fix else False if a.no_fix else bool(is_auto)
    corr = {"applied": apply_fix, "count": 0, "top": [], "files": []}
    if apply_fix:
        table, corr["files"] = load_corrections()
        clean, counts = apply_corrections(clean, table)
        corr["count"] = sum(counts.values())
        corr["top"] = [{"from": k, "to": v, "count": n}
                       for (k, v), n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:5]]
    paras = build_paragraphs(clean)
    if not paras:
        die(4, "Untertitel waren leer.")
    if backend == "text" and duration and clean[-1][0] > duration:
        print("Hinweis: --duration ist kleiner als der letzte Zeitstempel und wird ignoriert.", file=sys.stderr)
        duration = 0
    if not duration:
        duration = int(paras[-1][0]) + PARA_SECONDS
    words_total = sum(len(t.split()) for _, t in paras)

    base = os.path.join(a.out, vid)
    os.makedirs(os.path.join(base, "chunks"), exist_ok=True)
    for f in os.listdir(os.path.join(base, "chunks")):  # alte Chunks entfernen
        if f.endswith(".md"):
            os.remove(os.path.join(base, "chunks", f))

    title = info.get("title") or vid
    channel = info.get("channel") or info.get("uploader") or "?"
    ud = info.get("upload_date") or ""
    upload = f"{ud[:4]}-{ud[4:6]}-{ud[6:]}" if len(ud) == 8 else ud
    desc = (info.get("description") or "").strip()
    desc = desc[:1500] + (" ..." if len(desc) > 1500 else "")
    link = f"https://youtu.be/{vid}"
    kind = "automatisch" if is_auto else ("nativ" if backend == "supadata" else "manuell")

    ranges = make_ranges(chapters, duration, a.chunk_minutes)
    chunks = []
    for i, (s, e, t) in enumerate(ranges, 1):
        last = i == len(ranges)
        ps = [(st, tx) for st, tx in paras if st >= s and (st < e or last)]
        if not ps:
            continue
        w = sum(len(tx.split()) for _, tx in ps)
        fname = f"{len(chunks) + 1:02d}_{hms_compact(s)}-{hms_compact(e)}.md"
        head = [f"# {title} - Abschnitt {len(chunks) + 1}: {hms(s)} bis {hms(e)}"]
        if t:
            head.append(f"Kapitel: {t}")
        head.append(f"Video: {link} (Zeitlink: {link}?t=SEKUNDEN)\n")
        with open(os.path.join(base, "chunks", fname), "w", encoding="utf-8") as f:
            f.write("\n".join(head) + "\n\n".join(fmt_par(st, tx, a.plain) for st, tx in ps) + "\n")
        chunks.append({"file": f"chunks/{fname}", "start": int(s), "end": int(e),
                       "title": t, "words": w, "tokens": est_tokens(w)})

    lines = [f"# {title}", "",
             f"- Kanal: {channel}", f"- Hochgeladen: {upload}", f"- Dauer: {hms(duration)}",
             f"- Link: {link}", f"- Untertitelspur: {key} ({kind}, Format {fmt}, Quelle {source})",
             f"- Woerter: {words_total}, geschaetzte Tokens: {est_tokens(words_total)}"]
    if corr["applied"]:
        lines.append(f"- Korrekturen: automatisch korrigiert, Liste: corrections.tsv ({corr['count']} Ersetzungen)")
    if desc:
        lines += ["", "## Beschreibung (gekuerzt)", "", desc]
    if chapters:
        lines += ["", "## Kapitel", ""]
        lines += [f"- [{hms(c['start_time'])}] {c.get('title', '')}" for c in chapters]
    lines += ["", "## Transkript", ""]
    body = "\n\n".join(fmt_par(st, tx, a.plain) for st, tx in paras)
    with open(os.path.join(base, "transcript.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n" + body + "\n")

    meta = {"id": vid, "title": title, "channel": channel, "upload_date": upload, "duration": duration,
            "language": info.get("language"), "backend": backend, "track": key, "track_kind": kind, "source": source,
            "words": words_total, "est_tokens": est_tokens(words_total),
            "chapters": [{"start": c["start_time"], "title": c.get("title")} for c in chapters],
            "corrections": corr, "last_timestamp": int(clean[-1][0]), "chunks": chunks}
    with open(os.path.join(base, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # kompakte Zusammenfassung
    p = os.path.abspath(base)
    print(f"Titel:   {title}\nKanal:   {channel}  ({upload})\nDauer:   {hms(duration)}")
    print(f"Spur:    {key} ({kind}, {source})")
    print(f"Umfang:  {words_total} Woerter, ca. {est_tokens(words_total)} Tokens")
    if backend == "text":
        cov = f"letzter Zeitstempel {hms(clean[-1][0])}"
        if a.duration:
            cov += f" von {hms(duration)} ({min(100, int(clean[-1][0] * 100 / max(duration, 1)))} %)"
        print(f"Import:  {len(segs)} Eintraege, {panel['dupes']} Doppelte entfernt, {cov}")
    if corr["applied"]:
        top = ", ".join(f"{t['from']}→{t['to']} ×{t['count']}" for t in corr["top"])
        print(f"Korrekturen: {corr['count']} Ersetzungen" + (f" (Top: {top})" if top else ""))
    else:
        print("Korrekturen: aus (manuelle Spur oder --no-fix)")
    print(f"Datei:   {p}/transcript.md\nChunks:  {p}/chunks/ ({len(chunks)} Dateien)\nMeta:    {p}/meta.json")
    print("Nr | Datei | Zeit | Tokens | Kapitel")
    for n, c in enumerate(chunks[:20], 1):
        print(f"{n:02d} | {os.path.basename(c['file'])} | {hms(c['start'])}-{hms(c['end'])} | "
              f"{c['tokens']} | {c['title'][:60]}")
    if len(chunks) > 20:
        print(f"... {len(chunks) - 20} weitere Chunks, siehe meta.json")


if __name__ == "__main__":
    main()
