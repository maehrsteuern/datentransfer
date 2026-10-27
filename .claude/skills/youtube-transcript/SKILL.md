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
   `--list` (nur verfuegbare Spuren zeigen), `--cookies cookies.txt` (Altersfreigabe/Bot-Pruefung).

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
   und Zahlen koennen falsch transkribiert sein.

## Spurauswahl

Handgemachte Untertitel vor automatischen; Reihenfolge gemaess `--lang` (Standard de, dann en),
danach Originalsprache des Videos, danach irgendeine Spur. Maschinell uebersetzte
Auto-Spuren werden nur im Notfall genutzt. Letzter Fallback ist `youtube-transcript-api`.

## Fehlerbehebung

| Exit | Bedeutung / Abhilfe |
|------|---------------------|
| 2 | URL/ID nicht erkannt: Link pruefen (Playlist-Links brauchen `v=`). |
| 3 | Metadaten nicht abrufbar: Video privat/geloescht/altersbeschraenkt oder Bot-Pruefung. Einige Minuten warten, ggf. `--cookies cookies.txt` (exportiert aus dem Browser). |
| 4 | Keine Untertitel vorhanden (z. B. Livestream, deaktiviert). Mit `--list` pruefen; sonst Nutzer bitten, das Transkript einzufuegen (YouTube: "..." > "Transkript anzeigen"). |
| 5 | Download gescheitert, meist **HTTP 429**. Das Skript wiederholt automatisch nach 5/15/40 s. Danach 2-5 Minuten warten und erneut starten, nicht in Schleife wiederholen. |

- Nur **eine** Untertitelspur pro Aufruf wird geladen; weitere Sprachen erst nach einer Pause anfordern.
- **Claude.ai / Sandbox ohne Netzzugang zu YouTube:** Das Skript scheitert dann mit Exit 3.
  Nutzer bitten, das Transkript als Text/Datei einzufuegen oder die Netzwerkeinstellungen
  (Domain-Freigabe fuer youtube.com, googlevideo.com) zu pruefen.
- Cloud-/Rechenzentrums-IPs werden von YouTube haeufiger gedrosselt als Heimnetze.
- Ausgabeordner `yt-transcripts/` nicht committen (steht in `.gitignore`).
