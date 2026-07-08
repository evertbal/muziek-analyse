#!/usr/bin/env python3
"""Flask webapp voor Muziek-Analyse."""

import os
import json
import uuid
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file
from werkzeug.utils import secure_filename
import io

from config import (DB_PATH, AUDIO_EXTENSIONS, OUTPUT_DIR, SECRET_KEY,
                    YOUTUBE_DOWNLOAD_DIR, UPLOAD_DIR, MAX_UPLOAD_SIZE)
from models import init_db, create_track, get_track, list_tracks, search_tracks, delete_track, update_status, update_key
from tasks import enqueue, enqueue_youtube, enqueue_url, get_status

# Lazy imports to avoid slow numpy/librosa import at startup
def parse_filename_metadata(filename):
    from analyse import parse_filename_metadata as _parse
    return _parse(filename)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_SIZE

# Initialize database on startup
init_db()


@app.route('/')
def index():
    """Dashboard: track listing with search/filter."""
    q = request.args.get('q', '').strip()
    key_filter = request.args.get('key', '').strip()
    bpm_min = request.args.get('bpm_min', '').strip()
    bpm_max = request.args.get('bpm_max', '').strip()

    if q or key_filter or bpm_min or bpm_max:
        tracks = search_tracks(
            query=q or None,
            key=key_filter or None,
            bpm_min=int(bpm_min) if bpm_min else None,
            bpm_max=int(bpm_max) if bpm_max else None,
        )
    else:
        tracks = list_tracks()

    # Get pending/analysing tracks for status display
    all_tracks = list_tracks()
    active_tracks = [t for t in all_tracks if t['status'] in ('pending', 'analysing', 'downloading')]

    return render_template('index.html', tracks=tracks, active_tracks=active_tracks,
                          q=q, key_filter=key_filter, bpm_min=bpm_min, bpm_max=bpm_max)


@app.route('/tracks/<int:track_id>')
def track_detail(track_id):
    """Track detail page with full analysis."""
    track = get_track(track_id)
    if not track:
        flash('Track niet gevonden.', 'error')
        return redirect(url_for('index'))
    if track['status'] != 'done':
        flash('Analyse nog niet voltooid.', 'warning')
        return redirect(url_for('index'))

    analysis_data = track['analysis_data']
    # Pre-compute derived data for the template's JS
    from export import _prepare_template_data
    tpl_data = _prepare_template_data(track['name'], analysis_data)

    # Serialize for JS embedding in template
    js_data = {
        'uniqueChords': tpl_data['unique_chords'],
        'sectionsData': tpl_data['sections_data'],
        'allChords': tpl_data['all_chords'],
        'barsData': tpl_data['bars'],
        'globalRoles': {str(k): v for k, v in tpl_data['global_note_roles'].items()},
        'globalPhrases': tpl_data['global_phrases'],
        'duration': round(tpl_data['duration'], 2),
        'bpm': tpl_data['bpm'],
        'patterns': tpl_data['patterns'],
        'patternCharacters': tpl_data['pattern_characters'],
        'melody': tpl_data.get('melody'),
    }

    # Check filename metadata hints
    filename_hints = {}
    if track.get('filename_key') or track.get('filename_bpm'):
        detected_key = f"{analysis_data['key']}{'m' if analysis_data['mode'] == 'minor' else ''}"
        if track.get('filename_key'):
            fn_key = track['filename_key']
            if track.get('filename_bpm'):
                fn_key_full = fn_key
            else:
                fn_key_full = fn_key
            filename_hints['key'] = fn_key_full
            filename_hints['key_match'] = fn_key_full.rstrip('m') == analysis_data['key']
        if track.get('filename_bpm'):
            filename_hints['bpm'] = track['filename_bpm']
            filename_hints['bpm_match'] = abs(track['filename_bpm'] - analysis_data['bpm']) <= 3

    return render_template('track_detail.html', track=track, tpl_data=tpl_data,
                          js_data=json.dumps(js_data, ensure_ascii=False),
                          filename_hints=filename_hints)


@app.route('/tracks/<int:track_id>/export/html')
def export_html(track_id):
    """Download standalone HTML file."""
    track = get_track(track_id)
    if not track or not track['analysis_data']:
        flash('Track niet gevonden of nog niet geanalyseerd.', 'error')
        return redirect(url_for('index'))

    from export import generate_html
    html = generate_html(track['name'], track['analysis_data'])
    return send_file(
        io.BytesIO(html.encode('utf-8')),
        mimetype='text/html',
        as_attachment=True,
        download_name=f"{track['name']}.html"
    )


@app.route('/tracks/<int:track_id>/export/markdown')
def export_markdown(track_id):
    """Download Markdown file."""
    track = get_track(track_id)
    if not track or not track['analysis_data']:
        flash('Track niet gevonden of nog niet geanalyseerd.', 'error')
        return redirect(url_for('index'))

    from export import generate_markdown
    md = generate_markdown(track['name'], track['analysis_data'])
    return send_file(
        io.BytesIO(md.encode('utf-8')),
        mimetype='text/markdown',
        as_attachment=True,
        download_name=f"{track['name']}.md"
    )


@app.route('/youtube', methods=['POST'])
def youtube_import():
    """Import a track from YouTube URL."""
    from youtube import validate_youtube_url, extract_info

    url = request.form.get('youtube_url', '').strip()
    if not url:
        flash('Geen YouTube URL opgegeven.', 'error')
        return redirect(url_for('index'))

    if not validate_youtube_url(url):
        flash('Ongeldige YouTube URL.', 'error')
        return redirect(url_for('index'))

    try:
        info = extract_info(url)
    except Exception as e:
        flash(f'Kon video-info niet ophalen: {e}', 'error')
        return redirect(url_for('index'))

    title = info['title']
    # Use a placeholder file_path until download completes
    placeholder_path = f"youtube://{url}"
    track_id = create_track(title, placeholder_path, source='youtube', youtube_url=url)
    if track_id is None:
        flash(f'Track "{title}" bestaat al in de database.', 'warning')
        return redirect(url_for('index'))

    os.makedirs(YOUTUBE_DOWNLOAD_DIR, exist_ok=True)
    enqueue_youtube(track_id, url, YOUTUBE_DOWNLOAD_DIR, DB_PATH)
    flash(f'YouTube-import gestart voor "{title}".', 'success')
    return redirect(url_for('index'))


@app.route('/import-url', methods=['POST'])
def import_url():
    """Import a track from a direct link to an audio file."""
    from download import validate_audio_url, _derive_name

    url = request.form.get('audio_url', '').strip()
    if not url:
        flash('Geen URL opgegeven.', 'error')
        return redirect(url_for('index'))

    if not validate_audio_url(url):
        flash('Ongeldige URL. Gebruik een directe http(s)-link naar een audiobestand.', 'error')
        return redirect(url_for('index'))

    name = _derive_name(url)
    meta = parse_filename_metadata(name)
    fn_key = None
    if 'key' in meta:
        fn_key = meta['key'] + ('m' if meta.get('mode') == 'minor' else '')

    # Placeholder path (deduped on URL) until the download completes.
    placeholder_path = f"url://{url}"
    track_id = create_track(name, placeholder_path,
                            filename_key=fn_key,
                            filename_bpm=meta.get('bpm'),
                            source='url')
    if track_id is None:
        flash(f'Track "{name}" bestaat al in de database.', 'warning')
        return redirect(url_for('index'))

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    # Ad-hoc download: stored in UPLOAD_DIR and removed after analysis.
    enqueue_url(track_id, url, UPLOAD_DIR, DB_PATH)
    flash(f'Import gestart voor "{name}". Het bestand wordt na analyse verwijderd.', 'success')
    return redirect(url_for('index'))


@app.route('/upload', methods=['POST'])
def upload():
    """Upload an audio file, analyse it, then delete the file afterwards."""
    file = request.files.get('audio_file')
    if not file or not file.filename:
        flash('Geen bestand geselecteerd.', 'error')
        return redirect(url_for('index'))

    original_name = file.filename
    ext = Path(original_name).suffix.lower()
    if ext not in AUDIO_EXTENSIONS:
        flash(f'Niet-ondersteund formaat: {ext or "onbekend"}', 'error')
        return redirect(url_for('index'))

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    # Unique on-disk name to avoid collisions; original name kept for display.
    safe = secure_filename(original_name) or f'upload{ext}'
    file_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{safe}")
    file.save(file_path)
    file_size = os.path.getsize(file_path)

    name = Path(original_name).stem
    meta = parse_filename_metadata(original_name)
    fn_key = None
    if 'key' in meta:
        fn_key = meta['key'] + ('m' if meta.get('mode') == 'minor' else '')

    track_id = create_track(name, file_path, file_size,
                            filename_key=fn_key,
                            filename_bpm=meta.get('bpm'),
                            source='upload')
    if track_id is None:
        os.remove(file_path)
        flash(f'Track "{name}" bestaat al in de database.', 'warning')
        return redirect(url_for('index'))

    # delete_after=True: the uploaded file is removed once analysis finishes.
    enqueue(track_id, file_path, DB_PATH, delete_after=True)
    flash(f'Upload gestart voor "{name}". Het bestand wordt na analyse verwijderd.', 'success')
    return redirect(url_for('index'))


@app.errorhandler(413)
def file_too_large(e):
    """Friendly message when an upload exceeds MAX_CONTENT_LENGTH."""
    flash(f'Bestand te groot (max {MAX_UPLOAD_SIZE // (1024 * 1024)} MB).', 'error')
    return redirect(url_for('index'))


@app.route('/analyse/status/<int:track_id>')
def analyse_status(track_id):
    """JSON endpoint: poll analysis progress."""
    track = get_track(track_id)
    if not track:
        return jsonify({'status': 'error', 'step': 'Track niet gevonden'})

    status_info = get_status(track_id)
    return jsonify({
        'status': track['status'],
        'step': status_info['step'],
        'active': status_info['active'],
    })


@app.route('/tracks/<int:track_id>/delete', methods=['POST'])
def delete(track_id):
    """Delete a track from the database."""
    track = get_track(track_id)
    if track:
        # Remove downloaded file for YouTube tracks
        if track.get('source') == 'youtube' and track.get('file_path'):
            file_path = track['file_path']
            if not file_path.startswith('youtube://') and os.path.isfile(file_path):
                os.remove(file_path)
        delete_track(track_id)
        flash(f'"{track["name"]}" verwijderd.', 'success')
    return redirect(url_for('index'))


@app.route('/tracks/<int:track_id>/override-key', methods=['POST'])
def override_key(track_id):
    """Manually override the detected key and mode."""
    track = get_track(track_id)
    if not track:
        flash('Track niet gevonden.', 'error')
        return redirect(url_for('index'))

    new_key = request.form.get('key', '').strip()
    new_mode = request.form.get('mode', '').strip()

    valid_keys = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    if new_key not in valid_keys or new_mode not in ('major', 'minor'):
        flash('Ongeldige toonsoort opgegeven.', 'error')
        return redirect(url_for('track_detail', track_id=track_id))

    update_key(track_id, new_key, new_mode)
    mode_nl = 'mineur' if new_mode == 'minor' else 'majeur'
    flash(f'Toonsoort gewijzigd naar {new_key} {mode_nl}.', 'success')
    return redirect(url_for('track_detail', track_id=track_id))


@app.route('/tracks/<int:track_id>/reanalyse', methods=['POST'])
def reanalyse(track_id):
    """Re-run analysis on existing track."""
    track = get_track(track_id)
    if not track:
        flash('Track niet gevonden.', 'error')
        return redirect(url_for('index'))
    if not os.path.isfile(track['file_path']):
        flash(f'Audiobestand niet gevonden: {track["file_path"]}', 'error')
        return redirect(url_for('index'))

    update_status(track_id, 'pending')
    enqueue(track_id, track['file_path'], DB_PATH)
    flash(f'Heranalyse gestart voor "{track["name"]}".', 'success')
    return redirect(url_for('index'))


@app.template_filter('fmt_time')
def fmt_time_filter(seconds):
    """Format seconds to m:ss."""
    if seconds is None:
        return '—'
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"


@app.template_filter('fmt_size')
def fmt_size_filter(size):
    """Format file size in MB."""
    if size is None:
        return '—'
    return f"{size / 1024 / 1024:.1f} MB"


if __name__ == '__main__':
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(YOUTUBE_DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    port = int(os.environ.get('PORT', 5050))
    app.run(debug=True, host='0.0.0.0', port=port, threaded=True)
