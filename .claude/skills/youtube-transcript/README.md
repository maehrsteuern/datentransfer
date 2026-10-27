# Skill: youtube-transcript

Holt das vollstaendige Transkript eines YouTube-Videos (auch mehrstuendige), speichert es
als Datei und teilt es in Chunks, die Claude abschnittsweise einlesen kann.

## Installation in Claude Code

- **Im Repo:** Der Ordner `.claude/skills/youtube-transcript/` wird automatisch erkannt.
- **Global:** Ordner nach `~/.claude/skills/youtube-transcript/` kopieren.
- Voraussetzung: Python 3. `yt-dlp` wird bei Bedarf automatisch per `pip` installiert.
- Optional: Umgebungsvariable `SUPADATA_API_KEY` (siehe Abschnitt Backends).

## Installation in Claude.ai

1. Ordner `youtube-transcript/` zippen (SKILL.md liegt im Ordner-Stamm) oder `dist/youtube-transcript.zip` nutzen.
2. In Claude.ai: Einstellungen > Faehigkeiten (Capabilities) > Skills > Skill hochladen.
3. Code-Ausfuehrung muss aktiviert sein. Hat die Sandbox keinen Zugriff auf YouTube,
   fordert Claude zum Einfuegen des Transkripts auf.

## Manuell nutzen

```bash
python3 scripts/yt_transcript.py "https://youtu.be/dQw4w9WgXcQ" --lang de,en --out ./yt-transcripts
python3 scripts/yt_transcript.py dQw4w9WgXcQ --list
```

## Backends

`--backend auto|ytdlp|supadata` (Standard `auto`). Mit gesetztem `SUPADATA_API_KEY` nutzt `auto`
die bezahlte Supadata-API (nur vorhandene Untertitel, `mode=native`, ca. 1 Kontingent-Einheit pro
Aufruf); sonst `yt-dlp`. Das hilft, wenn YouTube Cloud-IPs mit "Sign in to confirm you're not a bot"
blockt. Der Key gehoert nur in die Umgebung (z. B. Netzwerk-Secrets), nie in den Chat oder ins Repo.

```bash
SUPADATA_API_KEY=... python3 scripts/yt_transcript.py dQw4w9WgXcQ --backend supadata --lang de,en
```

## Import-Modus (`--from-text`)

Fuer Faelle, in denen YouTube den Abruf blockt: Text aus dem Panel "Transkript anzeigen" kopieren
und ohne Netzwerkzugriff einlesen. Mehrere Etappen mit `--append` (Ueberlappung wird entdoppelt;
Rohtext in `yt-transcripts/<ID>/raw_panel.txt`).

```bash
python3 scripts/yt_transcript.py "<URL>" --from-text panel.txt --title "Titel" --duration 1:42:10
pbpaste | python3 scripts/yt_transcript.py "<URL>" --from-text -
python3 scripts/yt_transcript.py "<URL>" --from-text teil2.txt --append
```

Weitere Optionen: `--channel`, `--track-kind auto|manual`, `--track-lang de`.

## Korrekturliste

`corrections.tsv` (`falsch<TAB>richtig`, `#` = Kommentar) behebt typische Fehlhoerungen automatischer
Untertitel (z. B. Datef -> DATEV, Chatgpt -> ChatGPT). Ganze Woerter, Gross-/Kleinschreibung zaehlt;
Standard nur bei automatischen Spuren (`--fix` / `--no-fix`). Eigene Eintraege in
`~/.config/yt-transcript/corrections.tsv`. Nur Nicht-Woerter aufnehmen, damit korrekter Text nie veraendert wird.
