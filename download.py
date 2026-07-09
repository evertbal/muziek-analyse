"""Download-utility voor een directe link naar een audiobestand."""

import os
import uuid
import urllib.request
from urllib.parse import urlparse, unquote

from werkzeug.utils import secure_filename

from config import AUDIO_EXTENSIONS, MAX_UPLOAD_SIZE

# Content-Type -> extensie, voor het geval de URL zelf geen bruikbare extensie heeft.
_CONTENT_TYPE_EXT = {
    'audio/mpeg': '.mp3',
    'audio/mp3': '.mp3',
    'audio/wav': '.wav',
    'audio/x-wav': '.wav',
    'audio/wave': '.wav',
    'audio/flac': '.flac',
    'audio/x-flac': '.flac',
    'audio/ogg': '.ogg',
    'application/ogg': '.ogg',
    'audio/mp4': '.m4a',
    'audio/x-m4a': '.m4a',
    'audio/aiff': '.aiff',
    'audio/x-aiff': '.aiff',
}


def validate_audio_url(url):
    """Controleer of de URL een geldige http(s)-link is."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ('http', 'https') and bool(p.netloc)


def _guess_extension(url, content_type):
    """Bepaal de audio-extensie uit de URL of het Content-Type. None als onbekend."""
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in AUDIO_EXTENSIONS:
        return ext
    if content_type:
        ct = content_type.split(';')[0].strip().lower()
        if ct in _CONTENT_TYPE_EXT:
            return _CONTENT_TYPE_EXT[ct]
    return None


def _derive_name(url):
    """Leid een leesbare tracknaam af uit de bestandsnaam in de URL."""
    base = os.path.basename(unquote(urlparse(url).path))
    name = os.path.splitext(base)[0].strip()
    return name or 'audio'


def download_audio_url(url, output_dir, progress_callback=None):
    """Download een audiobestand vanaf een directe URL.

    Geeft een dict terug met file_path en title. Gooit ValueError bij een
    niet-ondersteund formaat of wanneer het bestand groter is dan MAX_UPLOAD_SIZE.
    """
    os.makedirs(output_dir, exist_ok=True)

    if progress_callback:
        progress_callback("Downloaden starten...")

    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        content_type = resp.headers.get('Content-Type', '')
        ext = _guess_extension(url, content_type)
        if ext is None:
            raise ValueError(
                "Geen ondersteund audioformaat gevonden op deze URL "
                "(verwacht mp3, wav, flac, ogg, m4a of aiff)."
            )

        declared = resp.headers.get('Content-Length')
        if declared and int(declared) > MAX_UPLOAD_SIZE:
            raise ValueError(
                f"Bestand te groot (max {MAX_UPLOAD_SIZE // (1024 * 1024)} MB)."
            )

        name = _derive_name(url)
        safe = secure_filename(name + ext) or f'audio{ext}'
        file_path = os.path.join(output_dir, f"{uuid.uuid4().hex}_{safe}")

        downloaded = 0
        try:
            with open(file_path, 'wb') as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_UPLOAD_SIZE:
                        raise ValueError(
                            f"Bestand te groot (max {MAX_UPLOAD_SIZE // (1024 * 1024)} MB)."
                        )
                    f.write(chunk)
                    if progress_callback:
                        mb = downloaded / (1024 * 1024)
                        progress_callback(f"Downloaden... {mb:.1f} MB")
        except Exception:
            if os.path.isfile(file_path):
                os.remove(file_path)
            raise

    return {'file_path': file_path, 'title': name}
