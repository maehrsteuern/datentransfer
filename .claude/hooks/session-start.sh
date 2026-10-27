#!/bin/bash
# Installiert yt-dlp fuer den youtube-transcript-Skill (nur in Cloud-Sessions).
set -euo pipefail
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
python3 -c "import yt_dlp" 2>/dev/null && exit 0
pip install -q --disable-pip-version-check yt-dlp >/dev/null 2>&1 || echo "yt-dlp-Installation fehlgeschlagen; das Skript versucht es beim Aufruf erneut." >&2
