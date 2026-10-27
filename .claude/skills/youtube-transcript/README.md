# Skill: youtube-transcript

Holt das vollstaendige Transkript eines YouTube-Videos (auch mehrstuendige), speichert es
als Datei und teilt es in Chunks, die Claude abschnittsweise einlesen kann.

## Installation in Claude Code

- **Im Repo:** Der Ordner `.claude/skills/youtube-transcript/` wird automatisch erkannt.
- **Global:** Ordner nach `~/.claude/skills/youtube-transcript/` kopieren.
- Voraussetzung: Python 3. `yt-dlp` wird bei Bedarf automatisch per `pip` installiert.

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
