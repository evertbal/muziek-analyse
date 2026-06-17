"""YouTube download utility via yt-dlp."""

import os
import re
import tempfile
import yt_dlp


# YouTube URL patterns
_YT_PATTERNS = [
    re.compile(r'^https?://(www\.)?youtube\.com/watch\?v=[\w-]+'),
    re.compile(r'^https?://youtu\.be/[\w-]+'),
    re.compile(r'^https?://(www\.)?youtube\.com/shorts/[\w-]+'),
    re.compile(r'^https?://music\.youtube\.com/watch\?v=[\w-]+'),
]


# Cache for cookies written from the YT_COOKIES env var
_cookie_file_cache = None


def validate_youtube_url(url):
    """Check if the given URL is a valid YouTube URL."""
    return any(p.match(url) for p in _YT_PATTERNS)


def _cookies_path():
    """Return a path to a Netscape cookies.txt, or None.

    Needed because YouTube blocks requests from datacenter IPs (e.g. Railway)
    with a "Sign in to confirm you're not a bot" error. Supports two env vars:

    - YT_COOKIES_FILE: path to an existing cookies.txt on disk
    - YT_COOKIES:      raw cookies.txt content (handy as a Railway variable);
                       written to a temp file on first use
    """
    global _cookie_file_cache

    path = os.environ.get('YT_COOKIES_FILE')
    if path and os.path.isfile(path):
        return path

    raw = os.environ.get('YT_COOKIES')
    if raw:
        if _cookie_file_cache and os.path.isfile(_cookie_file_cache):
            return _cookie_file_cache
        fd, tmp = tempfile.mkstemp(prefix='yt_cookies_', suffix='.txt')
        with os.fdopen(fd, 'w') as f:
            f.write(raw)
        _cookie_file_cache = tmp
        return tmp

    return None


def _base_opts():
    """Common yt-dlp options, including cookie/auth handling."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }

    cookies = _cookies_path()
    if cookies:
        opts['cookiefile'] = cookies

    # Optionally override the player client(s), e.g. YT_PLAYER_CLIENT="android,web".
    # Can help bypass bot detection without cookies, though results vary.
    client = os.environ.get('YT_PLAYER_CLIENT')
    if client:
        opts['extractor_args'] = {
            'youtube': {'player_client': [c.strip() for c in client.split(',') if c.strip()]}
        }

    return opts


def extract_info(url):
    """Extract metadata (title, duration) without downloading."""
    with yt_dlp.YoutubeDL(_base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            'title': info.get('title', 'Onbekend'),
            'duration': info.get('duration', 0),
        }


def download_audio(url, output_dir, progress_callback=None):
    """Download audio from YouTube and convert to mp3.

    Returns dict with file_path, title, duration.
    """
    result = {}

    def _progress_hook(d):
        if d['status'] == 'downloading' and progress_callback:
            pct = d.get('_percent_str', '').strip()
            progress_callback(f"Downloaden... {pct}")
        elif d['status'] == 'finished' and progress_callback:
            progress_callback("Converteren naar mp3...")

    opts = _base_opts()
    opts.update({
        'format': 'bestaudio/best',
        'outtmpl': f'{output_dir}/%(title)s.%(ext)s',
        'restrictfilenames': True,
        'progress_hooks': [_progress_hook],
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # yt-dlp changes extension after postprocessing
        filename = ydl.prepare_filename(info)
        # Replace original extension with .mp3
        base, _ = os.path.splitext(filename)
        mp3_path = base + '.mp3'

        result = {
            'file_path': mp3_path,
            'title': info.get('title', 'Onbekend'),
            'duration': info.get('duration', 0),
        }

    return result
