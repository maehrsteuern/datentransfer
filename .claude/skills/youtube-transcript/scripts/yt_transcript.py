#!/usr/bin/env python3
"""YouTube-Transkript holen, bereinigen, speichern und in Chunks aufteilen.

Aufruf:
    yt_transcript.py URL [--lang de,en] [--out DIR] [--chunk-minutes 20] [--plain] [--list]

Ausgabe (Standard: ./yt-transcripts/<VIDEO_ID>/):
    transcript.md   Kopf mit Metadaten + Kapitel + kompletter Text
    chunks/*.md     Abschnitte (nach Kapiteln, sonst nach --chunk-minutes)
    meta.json       Metadaten, gewaehlte Spur, Chunk-Index

Auf stdout erscheint nur eine kompakte Zusammenfassung (nie das Transkript).

Exit-Codes:
    0 OK | 1 unerwarteter Fehler | 2 ungueltige URL/ID | 3 Video-Metadaten nicht abrufbar
    4 keine Untertitel verfuegbar | 5 Untertitel-Download fehlgeschlagen (z. B. HTTP 429)

Spurauswahl (siehe choose_track): Handgemachte Untertitel (manuell) werden vor
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
from urllib.parse import parse_qs, urlparse

BACKOFF = (5, 15, 40)
PARA_SECONDS = 30
TOKENS_PER_WORD = 1.4


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


def clean_segments(segs):
    """Entfernt Rolling-/Doppel-Fragmente (typisch bei Auto-Untertiteln)."""
    out = []  # [start, text]
    for start, text in segs:
        text = text.replace("​", "").strip()
        text = re.sub(r"\[(?:Music|Musik|Applause|Applaus)\]", "", text, flags=re.I).strip()
        if not text:
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
    a = ap.parse_args()

    vid = parse_video_id(a.url)
    if not vid:
        die(2, "Keine gueltige YouTube-URL oder Video-ID erkannt.")
    langs = [l.strip().lower() for l in a.lang.split(",") if l.strip()] or ["de", "en"]

    ensure_yt_dlp()
    try:
        ydl, info = fetch_info(vid, a.cookies)
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace("ERROR: ", "").splitlines()[0][:300]
        hint = ""
        low = msg.lower()
        if "not a bot" in low:
            hint = " (YouTube-Bot-Pruefung/IP-Drosselung: einige Minuten warten, evtl. --cookies cookies.txt angeben)"
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
               "Bitte Transkript manuell einfuegen (YouTube: '...' > 'Transkript anzeigen').")

    clean = clean_segments(segs)
    paras = build_paragraphs(clean)
    if not paras:
        die(4, "Untertitel waren leer.")
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
    kind = "automatisch" if is_auto else "manuell"

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
            "language": info.get("language"), "track": key, "track_kind": kind, "source": source,
            "words": words_total, "est_tokens": est_tokens(words_total),
            "chapters": [{"start": c["start_time"], "title": c.get("title")} for c in chapters],
            "chunks": chunks}
    with open(os.path.join(base, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # kompakte Zusammenfassung
    p = os.path.abspath(base)
    print(f"Titel:   {title}\nKanal:   {channel}  ({upload})\nDauer:   {hms(duration)}")
    print(f"Spur:    {key} ({kind}, {source})")
    print(f"Umfang:  {words_total} Woerter, ca. {est_tokens(words_total)} Tokens")
    print(f"Datei:   {p}/transcript.md\nChunks:  {p}/chunks/ ({len(chunks)} Dateien)\nMeta:    {p}/meta.json")
    print("Nr | Datei | Zeit | Tokens | Kapitel")
    for n, c in enumerate(chunks[:20], 1):
        print(f"{n:02d} | {os.path.basename(c['file'])} | {hms(c['start'])}-{hms(c['end'])} | "
              f"{c['tokens']} | {c['title'][:60]}")
    if len(chunks) > 20:
        print(f"... {len(chunks) - 20} weitere Chunks, siehe meta.json")


if __name__ == "__main__":
    main()
