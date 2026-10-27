---
name: youtube-transcript
description: Holt das vollstaendige Transkript eines YouTube-Videos (auch mehrstuendige Podcasts) aus einer URL oder Video-ID, speichert es als Datei und liest es abschnittsweise ein. Nutzen bei YouTube-Links (watch, youtu.be, shorts, live, embed), "Transkript", "Untertitel", "Video zusammenfassen", "was sagt er im Video", "was wird bei Minute X gesagt", "Zitat aus dem Video" oder wenn der Inhalt eines YouTube-Videos analysiert werden soll.
---

# YouTube-Transkript

Holt Untertitel per `yt-dlp`, bereinigt sie (keine doppelten Auto-Untertitel-Fragmente),
gruppiert sie in Absaetze mit Zeitstempel `[HH:MM:SS]` und teilt sie in Chunks
(nach Kapiteln, sonst alle 20 Minuten).

## Ablauf

1. **Skript ausfuehren** (URL in jeder Form oder nackte 11-stellige ID):

   ```bash
   python3 .claude/skills/youtube-transcript/scripts/yt_transcript.py "<URL>" --lang de,en
   ```

   Pfad ggf. an den Skill-Ordner anpassen (z. B. `~/.claude/skills/youtube-transcript/...`).
   Fehlt `yt-dlp`, installiert das Skript es selbst. Ausgabe landet in `./yt-transcripts/<ID>/`.

   Optionen: `--out DIR`, `--chunk-minutes 20`, `--plain` (ohne Zeitstempel),
   `--list` (nur verfuegbare Spuren zeigen), `--cookies cookies.txt` (Altersfreigabe/Bot-Pruefung),
   `--backend auto|ytdlp|supadata` (siehe Backends), `--fix`/`--no-fix` (Korrekturliste), `--from-text` (siehe Fallback-Reihenfolge).

2. **Zusammenfassung lesen.** Das Skript druckt nur Metadaten, die gewaehlte Spur
   (manuell oder automatisch), Woerter, geschaetzte Tokens und eine Chunk-Tabelle
   (Datei, Zeitbereich, Kapitel, Tokens). Das Transkript selbst wird nie ausgegeben.

3. **Einlesen je nach Umfang:**
   - **<= ca. 25.000 Tokens:** `transcript.md` komplett mit Read lesen.
   - **Groesser:** Chunk-Dateien in `chunks/` nacheinander lesen (je ca. 3-7k Tokens).
     Nach jedem Chunk Notizen/Zwischenstand festhalten, nicht alles gleichzeitig laden.
   - **Gezielte Frage** ("Was sagt er zu X?", "Minute 45"): Anhand der Kapitel/Zeiten in der
     Tabelle nur die passenden Chunks lesen; sonst vorher mit Grep im Ordner nach Stichwoertern suchen.
   - **Zusammenfassung sehr langer Videos (> ca. 100k Tokens):** Chunk-Gruppen (z. B. 3-4 Chunks)
     parallel an Subagenten geben, die je Gruppe eine Kurzfassung mit Zeitstempeln liefern;
     danach die Teilzusammenfassungen zusammenfuehren.

4. **Antworten und zitieren.** Aussagen mit Zeitstempel belegen und verlinken:
   `https://youtu.be/<ID>?t=<SEKUNDEN>` (Sekunden = `HH*3600 + MM*60 + SS`),
   z. B. `[01:05:56](https://youtu.be/ABC123def45?t=3956)`.
   Woertliche Zitate kurz halten; sonst sinngemaess zusammenfassen.
   Kennzeichnen, wenn die Spur **automatisch** erzeugt wurde: Eigennamen, Fachbegriffe
   und Zahlen koennen falsch transkribiert sein (siehe Korrekturen).

## Fallback-Reihenfolge

1. **Skript** mit `--backend auto` (Standard).
2. Scheitert es an der Bot-Pruefung (Exit 3) und ist **kein** `SUPADATA_API_KEY` gesetzt:
   **Browser-Pfad, nur wenn ein Browser-Tool verfuegbar ist** (Claude in Chrome / eingebauter Browser).
3. Sonst den Nutzer bitten, das Transkript einzufuegen (YouTube: "..." > "Transkript anzeigen",
   Text markieren und kopieren), in eine Datei bzw. per stdin an `--from-text -` geben.

### Import-Modus `--from-text FILE|-`

```bash
python3 .../yt_transcript.py "<URL>" --from-text stage1.txt --title "..." --channel "..." --duration 1:42:10
python3 .../yt_transcript.py "<URL>" --from-text stage2.txt --append   # weiterer Abschnitt, Ueberlappung wird entdoppelt
```

Kein Netzwerkzugriff. Erkennt `0:00` + Textzeile, `0:00 Text`, `1:02:33`, Barrierefreiheits-Zeilen
("2 Minuten, 5 Sekunden"), Kapitelueberschriften und UI-Rauschen ("Transkript", "Kapitel" ...).
"Deutsch (automatisch erzeugt)" setzt Spur auto/de; sonst `--track-kind auto|manual`, `--track-lang`.
Rohtext liegt in `yt-transcripts/<ID>/raw_panel.txt`. Kapitel aus dem Panel steuern das Chunking.
Ohne `--duration` ist die Dauer nur geschaetzt; mit ihr zeigt die Zusammenfassung die Abdeckung in %.

### Browser-Pfad (strikte Regeln)

- Video oeffnen, "...mehr" > "Transkript anzeigen" mit **normalen Klicks**.
- Panel-Text mit dem Seitentext-Tool lesen. Der Seitentext wird bei ca. 50.000 Zeichen abgeschnitten:
  **in Etappen** lesen (Panel scrollen), jede Etappe in eine Datei speichern, dann
  `--from-text stage1.txt` und fuer jede weitere Etappe `--from-text stageN.txt --append`.
- **Abdeckung pruefen:** letzter Zeitstempel ca. gleich Videodauer (`--duration` angeben), sonst weiterlesen.
- **Niemals JavaScript ausfuehren oder die Seite veraendern ohne ausdrueckliche Zustimmung des Nutzers
  in dieser Unterhaltung.** Vorher fragen und den Grund erklaeren.
- Nie Kontoeinstellungen aendern, nie anmelden, nie Captchas/Bot-Pruefungen loesen: stoppen und berichten.

## Korrekturen (ASR-Fehlhoerungen)

`corrections.tsv` (neben `scripts/`): pro Zeile `falsch<TAB>richtig`, `#` = Kommentar, z. B.
`Datef` -> `DATEV`. Ganze Woerter, Gross-/Kleinschreibung zaehlt (`Datefs` bleibt unveraendert,
`Clotilde` ebenfalls). Standard: nur bei **automatischen** Spuren; `--fix`/`--no-fix` erzwingen.
Eigene Eintraege in `~/.config/yt-transcript/corrections.tsv` (gleiches Format, hat Vorrang).
Nur Schreibweisen aufnehmen, die keine echten deutschen Woerter sind. Zusammenfassung, `meta.json` und
Kopf von `transcript.md` nennen die Zahl der Ersetzungen; die Rohdaten bleiben ohne `--fix` unveraendert.

## Backends

- `--backend auto` (Standard): Ist `SUPADATA_API_KEY` gesetzt, wird die Supadata-API genutzt, sonst `yt-dlp`.
  Scheitert `yt-dlp` an der Bot-Pruefung, weist das Skript auf den Key hin (Exit 3).
- `supadata`: bezahlte Transkript-API, nur **vorhandene** Untertitel (`mode=native`, keine KI-Erzeugung).
  Braucht kein `yt-dlp`. Kosten ca. 1 Kontingent-Einheit pro Aufruf (Transkript max. 2 Aufrufe, plus 1 Metadaten-Aufruf).
  `--list` zeigt die verfuegbaren Sprachen (1 Aufruf).
- Key nur als Umgebungsvariable `SUPADATA_API_KEY` setzen (Umgebungseinstellungen > Netzwerk-Secrets),
  **nie im Chat einfuegen oder in Dateien schreiben**. Fehlt er, meldet das Skript das klar (Exit 3).
- Kapitel werden bei Supadata aus Zeitstempel-Zeilen der Beschreibung abgeleitet (erstes bei 0:00, mind. 3).

## Spurauswahl (yt-dlp)

Handgemachte Untertitel vor automatischen; Reihenfolge gemaess `--lang` (Standard de, dann en),
danach Originalsprache des Videos, danach irgendeine Spur. Maschinell uebersetzte
Auto-Spuren werden nur im Notfall genutzt. Letzter Fallback ist `youtube-transcript-api`.

## Fehlerbehebung

| Exit | Bedeutung / Abhilfe |
|------|---------------------|
| 2 | URL/ID nicht erkannt: Link pruefen (Playlist-Links brauchen `v=`). |
| 3 | Metadaten nicht abrufbar (oder `SUPADATA_API_KEY` fehlt/ungueltig): Video privat/geloescht/altersbeschraenkt oder Bot-Pruefung. Einige Minuten warten, ggf. `--cookies cookies.txt`; sonst Fallback-Reihenfolge (Browser-Pfad bzw. Einfuegen + `--from-text`). |
| 4 | Keine Untertitel vorhanden (z. B. Livestream, deaktiviert). Mit `--list` pruefen; sonst Nutzer bitten, das Transkript einzufuegen (YouTube: "..." > "Transkript anzeigen") und mit `--from-text -` einlesen. |
| 5 | Download gescheitert, meist **HTTP 429**. Das Skript wiederholt automatisch nach 5/15/40 s. Danach 2-5 Minuten warten und erneut starten, nicht in Schleife wiederholen. |

- Nur **eine** Untertitelspur pro Aufruf wird geladen; weitere Sprachen erst nach einer Pause anfordern.
- **Claude.ai / Sandbox ohne Netzzugang zu YouTube:** Das Skript scheitert dann mit Exit 3.
  Nutzer bitten, das Transkript als Text/Datei einzufuegen oder die Netzwerkeinstellungen
  (Domain-Freigabe fuer youtube.com, googlevideo.com) zu pruefen.
- Cloud-/Rechenzentrums-IPs werden von YouTube haeufiger gedrosselt als Heimnetze.
- Ausgabeordner `yt-transcripts/` nicht committen (steht in `.gitignore`).
