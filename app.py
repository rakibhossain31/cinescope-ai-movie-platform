import csv
import json
import os
import re
import sqlite3
from datetime import datetime
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'users.db'
DATA_PATH = BASE_DIR / 'data' / 'movies.csv'
APP_SECRET = os.environ.get('FLASK_SECRET_KEY', 'movie-project-secret-key')
TMDB_API_KEY = os.environ.get('TMDB_API_KEY', '').strip()
TMDB_BEARER_TOKEN = os.environ.get('TMDB_BEARER_TOKEN', '').strip()
AI_PROVIDER = os.environ.get('AI_PROVIDER', 'gemini').strip().lower()
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash').strip()
TMDB_BASE = 'https://api.themoviedb.org/3'
TMDB_IMAGE = 'https://image.tmdb.org/t/p'

app = Flask(__name__)
app.secret_key = APP_SECRET

GENRE_ID_MAP = {
    'Action': 28,
    'Adventure': 12,
    'Animation': 16,
    'Comedy': 35,
    'Crime': 80,
    'Drama': 18,
    'Family': 10751,
    'Fantasy': 14,
    'History': 36,
    'Horror': 27,
    'Music': 10402,
    'Mystery': 9648,
    'Romance': 10749,
    'Science Fiction': 878,
    'Thriller': 53,
    'War': 10752,
}
GENRE_NAME_MAP = {v: k for k, v in GENRE_ID_MAP.items()}
LANG_LABELS = {
    'en': 'English',
    'hi': 'Hindi',
    'bn': 'Bangla',
    'ko': 'Korean',
    'ja': 'Japanese',
}


def load_local_movies():
    movies = []
    if not DATA_PATH.exists():
        return movies
    with open(DATA_PATH, newline='', encoding='utf-8') as f:
        for idx, row in enumerate(csv.DictReader(f), start=1):
            genre = row.get('genre', 'Drama')
            lang = row.get('language', 'English')
            code = next((k for k, v in LANG_LABELS.items() if v.lower() == lang.lower()), 'en')
            movies.append({
                'id': idx,
                'source': 'local',
                'title': row.get('title', 'Untitled'),
                'overview': row.get('overview', ''),
                'rating': float(row.get('rating', 0) or 0),
                'vote_count': int(float(row.get('vote_count', 0) or 0)),
                'year': str(row.get('year', '') or '—'),
                'release_date': str(row.get('release_date', '') or ''),
                'language': lang,
                'language_code': code,
                'genres': [genre],
                'genre_ids': [GENRE_ID_MAP.get(genre, 18)],
                'mood': row.get('mood', ''),
                'poster': row.get('poster', ''),
                'backdrop': row.get('backdrop', ''),
                'runtime': int(float(row.get('runtime', 0) or 0)) if row.get('runtime') else None,
                'popularity': float(row.get('popularity', 0) or 0),
            })
    return movies


LOCAL_MOVIES = load_local_movies()
LOCAL_MOVIE_MAP = {m['id']: m for m in LOCAL_MOVIES}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            favorite_movies TEXT DEFAULT '',
            preferred_genres TEXT DEFAULT '',
            preferred_languages TEXT DEFAULT '',
            preferred_moods TEXT DEFAULT '',
            created_at TEXT,
            last_login TEXT
        )
        '''
    )
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS login_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL,
            login_time TEXT NOT NULL
        )
        '''
    )
    row = conn.execute("SELECT id, password FROM users WHERE username='admin'").fetchone()
    if row is None:
        conn.execute(
            'INSERT INTO users (username, password, role, created_at) VALUES (?, ?, ?, ?)',
            ('admin', generate_password_hash('admin123'), 'admin', datetime.utcnow().isoformat(timespec='seconds')),
        )
    elif not str(row['password']).startswith(('pbkdf2:', 'scrypt:')):
        conn.execute('UPDATE users SET password=? WHERE id=?', (generate_password_hash('admin123'), row['id']))
    conn.commit()
    conn.close()


init_db()


def current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    conn = db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    conn.close()
    return user


def parse_csv_field(value):
    return [x.strip() for x in str(value or '').split(',') if x.strip()]


def verify_password(stored, given):
    if stored.startswith(('pbkdf2:', 'scrypt:')):
        return check_password_hash(stored, given)
    return stored == given


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None or user['role'] != 'admin':
            flash('Only admin can access the admin dashboard.')
            return redirect(url_for('index'))
        return fn(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_globals():
    return {
        'current_user': current_user(),
        'tmdb_live': tmdb_enabled(),
        'ai_live': gemini_enabled(),
        'genre_options': sorted(GENRE_ID_MAP.items(), key=lambda x: x[0]),
    }


def tmdb_enabled():
    return bool(TMDB_BEARER_TOKEN or TMDB_API_KEY)


def gemini_enabled():
    return AI_PROVIDER == 'gemini' and bool(GEMINI_API_KEY)


def tmdb_headers():
    headers = {'accept': 'application/json'}
    if TMDB_BEARER_TOKEN:
        headers['Authorization'] = f'Bearer {TMDB_BEARER_TOKEN}'
    return headers


def tmdb_get(endpoint, params=None):
    if not tmdb_enabled():
        return None
    params = dict(params or {})
    if TMDB_API_KEY and not TMDB_BEARER_TOKEN:
        params['api_key'] = TMDB_API_KEY
    try:
        r = requests.get(f'{TMDB_BASE}{endpoint}', headers=tmdb_headers(), params=params, timeout=18)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def img(path, size='w500'):
    return f'{TMDB_IMAGE}/{size}{path}' if path else ''


def normalize_tmdb(item):
    genre_ids = item.get('genre_ids') or [g.get('id') for g in item.get('genres', []) if g.get('id')]
    genres = [GENRE_NAME_MAP.get(g, 'Unknown') for g in genre_ids] if genre_ids else [g.get('name', 'Unknown') for g in item.get('genres', [])]
    title = item.get('title') or item.get('name') or 'Untitled'
    rd = item.get('release_date') or item.get('first_air_date') or ''
    lc = (item.get('original_language') or '').lower()
    return {
        'id': int(item.get('id', 0)),
        'source': 'tmdb',
        'title': title,
        'overview': item.get('overview') or '',
        'rating': round(float(item.get('vote_average') or 0), 1),
        'vote_count': int(item.get('vote_count') or 0),
        'year': rd[:4] if rd else '—',
        'release_date': rd,
        'language': LANG_LABELS.get(lc, lc.upper() if lc else 'Unknown'),
        'language_code': lc,
        'genres': genres,
        'genre_ids': genre_ids,
        'mood': '',
        'poster': img(item.get('poster_path'), 'w500'),
        'backdrop': img(item.get('backdrop_path'), 'w780'),
        'runtime': item.get('runtime'),
        'popularity': float(item.get('popularity') or 0),
    }


def dedupe_movies(movies):
    seen = set()
    out = []
    for m in movies:
        key = (m.get('source'), m.get('id'), m.get('title', '').lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def home_sections():
    if tmdb_enabled():
        trending = tmdb_get('/trending/movie/week') or {}
        popular = tmdb_get('/movie/popular') or {}
        top = tmdb_get('/movie/top_rated') or {}
        return {
            'trending': [normalize_tmdb(x) for x in trending.get('results', [])[:10]],
            'popular': [normalize_tmdb(x) for x in popular.get('results', [])[:10]],
            'top_rated': [normalize_tmdb(x) for x in top.get('results', [])[:10]],
        }
    ordered = sorted(LOCAL_MOVIES, key=lambda m: (m['rating'], m['vote_count']), reverse=True)
    return {'trending': ordered[:10], 'popular': ordered[:10], 'top_rated': ordered[:10]}


def local_search(query='', genre='', year='', language='', rating='0', sort='popularity.desc'):
    items = list(LOCAL_MOVIES)
    q = (query or '').lower().strip()
    if q:
        items = [m for m in items if q in m['title'].lower() or q in m['overview'].lower()]
    if genre:
        try:
            gid = int(genre)
            items = [m for m in items if gid in m['genre_ids']]
        except Exception:
            pass
    if year:
        items = [m for m in items if str(m['year']) == str(year)]
    if language:
        items = [m for m in items if m['language_code'] == language or m['language'].lower() == language.lower()]
    min_rating = float(rating or 0)
    items = [m for m in items if m['rating'] >= min_rating]
    if sort == 'release_date.desc':
        items.sort(key=lambda m: m['release_date'] or '', reverse=True)
    else:
        items.sort(key=lambda m: (m['rating'], m['popularity']), reverse=True)
    return {'results': items, 'page': 1, 'total_pages': 1, 'total_results': len(items)}


def search_catalog(query='', genre='', year='', language='', rating='0', sort='popularity.desc', page='1'):
    if not tmdb_enabled():
        return local_search(query, genre, year, language, rating, sort)
    page = int(page or 1)
    min_rating = float(rating or 0)
    if query:
        data = tmdb_get('/search/movie', {'query': query, 'include_adult': 'false', 'page': page}) or {}
        results = [normalize_tmdb(x) for x in data.get('results', [])]
        filtered = []
        for m in results:
            if genre and int(genre) not in m['genre_ids']:
                continue
            if year and str(year) != m['year']:
                continue
            if language and m['language_code'] != language:
                continue
            if m['rating'] < min_rating:
                continue
            filtered.append(m)
        return {'results': filtered, 'page': data.get('page', 1), 'total_pages': data.get('total_pages', 1), 'total_results': len(filtered)}
    params = {'include_adult': 'false', 'sort_by': sort, 'page': page}
    if genre:
        params['with_genres'] = genre
    if year:
        params['primary_release_year'] = year
    if language:
        params['with_original_language'] = language
    if min_rating:
        params['vote_average.gte'] = min_rating
        params['vote_count.gte'] = 50
    data = tmdb_get('/discover/movie', params) or {}
    results = [normalize_tmdb(x) for x in data.get('results', [])]
    return {'results': results, 'page': data.get('page', 1), 'total_pages': data.get('total_pages', 1), 'total_results': data.get('total_results', len(results))}


def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def local_recommend(user, candidates, filters=None, limit=5):
    filters = filters or {}
    favs = parse_csv_field(user['favorite_movies'])
    pref_genres = {x.strip().lower() for x in parse_csv_field(user['preferred_genres'])}
    pref_langs = {x.strip().lower() for x in parse_csv_field(user['preferred_languages'])}
    pref_moods = {x.strip().lower() for x in parse_csv_field(user['preferred_moods'])}
    if not any([favs, pref_genres, pref_langs, pref_moods]):
        return []
    genre_name = str(filters.get('genre_name', '')).lower()
    query = str(filters.get('query', '')).lower()
    lang = str(filters.get('language', '')).lower()
    scored = []
    for m in candidates:
        score = float(m.get('rating', 0)) * 10 + min(float(m.get('popularity', 0)) / 100, 5)
        genres_lower = {g.lower() for g in m.get('genres', [])}
        reasons = []
        if pref_genres & genres_lower:
            score += 20
            reasons.append('genre match')
        if pref_langs and (m.get('language', '').lower() in pref_langs or m.get('language_code', '').lower() in pref_langs):
            score += 10
            reasons.append('language match')
        if genre_name and genre_name in genres_lower:
            score += 10
            reasons.append('current genre fit')
        if lang and (m.get('language_code') == lang or m.get('language', '').lower() == lang):
            score += 6
        if query and query in m.get('title', '').lower():
            score += 12
            reasons.append('search overlap')
        if query and query in m.get('overview', '').lower():
            score += 4
        for fav in favs:
            score += similarity(fav, m.get('title', '')) * 18
        text_blob = ' '.join([m.get('overview', ''), ' '.join(m.get('genres', [])), m.get('mood', '')]).lower()
        for mood in pref_moods:
            if mood in text_blob:
                score += 7
                reasons.append('mood fit')
        enriched = dict(m)
        enriched['ai_score'] = round(score, 1)
        enriched['reason'] = 'Strong ' + ', '.join(dict.fromkeys(reasons).keys() if hasattr(dict.fromkeys(reasons), 'keys') else reasons[:3]) if reasons else 'Good match for your saved taste profile.'
        if enriched['reason'] == 'Strong ':
            enriched['reason'] = 'Good match for your saved taste profile.'
        enriched['why_now'] = 'It lines up with your saved favorites, genre choices, and current browsing context.'
        scored.append(enriched)
    scored.sort(key=lambda x: (x['ai_score'], x.get('rating', 0)), reverse=True)
    return scored[:limit]


def gemini_rerank(user, candidates, filters=None, limit=5):
    if not gemini_enabled() or not candidates:
        return []
    profile = {
        'favorite_movies': parse_csv_field(user['favorite_movies']),
        'preferred_genres': parse_csv_field(user['preferred_genres']),
        'preferred_languages': parse_csv_field(user['preferred_languages']),
        'preferred_moods': parse_csv_field(user['preferred_moods']),
        'current_search': filters or {},
    }
    shortlist = []
    for m in candidates[:20]:
        shortlist.append({
            'id': m['id'], 'source': m['source'], 'title': m['title'], 'genres': m['genres'],
            'language': m['language'], 'year': m['year'], 'rating': m['rating'], 'overview': m['overview'][:350]
        })
    prompt = (
        'You are a movie recommender inside a Flask website. Pick only from the provided candidate list. '
        'Return strict JSON in this format: '
        '{"recommendations":[{"id":123,"source":"tmdb","score":95,"reason":"...","why_now":"..."}]}. '
        f'Limit to {limit} items. User profile: {json.dumps(profile, ensure_ascii=False)}. '
        f'Candidate movies: {json.dumps(shortlist, ensure_ascii=False)}'
    )
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}'
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0},
    }
    try:
        r = requests.post(url, json=payload, timeout=25)
        r.raise_for_status()
        data = r.json()
        text = data['candidates'][0]['content']['parts'][0]['text']
        parsed = json.loads(text)
    except Exception:
        return []
    lookup = {(m['source'], m['id']): m for m in candidates}
    picks = []
    seen = set()
    for item in parsed.get('recommendations', [])[:limit]:
        key = (item.get('source', 'tmdb'), int(item.get('id', 0) or 0))
        if key in seen or key not in lookup:
            continue
        seen.add(key)
        m = dict(lookup[key])
        m['ai_score'] = round(float(item.get('score', 90)), 1)
        m['reason'] = item.get('reason', 'Picked by Gemini from your taste profile.')
        m['why_now'] = item.get('why_now', 'It balances your saved taste and current browsing context.')
        picks.append(m)
    return picks



GENRE_ALIASES = {
    'sci fi': 'Science Fiction',
    'sci-fi': 'Science Fiction',
    'scifi': 'Science Fiction',
    'science fiction': 'Science Fiction',
    'romcom': 'Romance',
    'rom-com': 'Romance',
    'thrillers': 'Thriller',
    'comedies': 'Comedy',
    'romances': 'Romance',
    'animations': 'Animation',
    'actions': 'Action',
}


def safe_json_parse(text):
    if not text:
        return None
    text = str(text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r'```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            return None
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


def search_people_tmdb(name, limit=5):
    if not tmdb_enabled() or not name.strip():
        return []
    data = tmdb_get('/search/person', {'query': name.strip(), 'include_adult': 'false', 'page': 1}) or {}
    return data.get('results', [])[:limit]


def person_known_movies(person, limit=5):
    movies = []
    for item in person.get('known_for', []):
        if item.get('media_type') == 'movie' or item.get('title'):
            movies.append(normalize_tmdb(item))
    if tmdb_enabled() and person.get('id') and len(movies) < limit:
        credits = tmdb_get(f"/person/{person['id']}/movie_credits") or {}
        cast_items = [normalize_tmdb(item) for item in credits.get('cast', []) if item.get('id')]
        cast_items.sort(key=lambda m: (m.get('popularity', 0), m.get('vote_count', 0), m.get('rating', 0)), reverse=True)
        movies.extend(cast_items[:12])
    return dedupe_movies(movies)[:limit]


def resolve_person_candidate(name, limit=5):
    results = search_people_tmdb(name, limit=max(limit, 6))
    if not results:
        return None, []
    scored = sorted(
        [(similarity(name, item.get('name', '')), item) for item in results],
        key=lambda x: (x[0], float(x[1].get('popularity', 0) or 0)),
        reverse=True,
    )
    best_score, best_person = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    if best_score >= 0.82 or best_score - second_score >= 0.16:
        return best_person, [item for _, item in scored[:limit]]
    return None, [item for _, item in scored[:limit]]


def person_common_movies(name_a, name_b, limit=5):
    people_a = search_people_tmdb(name_a, limit=3)
    people_b = search_people_tmdb(name_b, limit=3)
    if not people_a or not people_b:
        return []
    aggregated = {}
    for person_a in people_a:
        credits_a = tmdb_get(f"/person/{person_a['id']}/movie_credits") or {}
        map_a = {item.get('id'): item for item in credits_a.get('cast', []) if item.get('id')}
        if not map_a:
            continue
        score_a = similarity(name_a, person_a.get('name', ''))
        for person_b in people_b:
            credits_b = tmdb_get(f"/person/{person_b['id']}/movie_credits") or {}
            map_b = {item.get('id'): item for item in credits_b.get('cast', []) if item.get('id')}
            if not map_b:
                continue
            score_b = similarity(name_b, person_b.get('name', ''))
            for movie_id in set(map_a) & set(map_b):
                base = map_a[movie_id]
                movie = normalize_tmdb(base)
                combined = (score_a + score_b) * 100 + float(movie.get('popularity', 0)) + float(movie.get('vote_count', 0)) / 100
                current = aggregated.get(movie_id)
                if current is None or combined > current['score']:
                    aggregated[movie_id] = {'score': combined, 'movie': movie}
    movies = [item['movie'] for item in aggregated.values()]
    movies.sort(key=lambda m: (m.get('popularity', 0), m.get('rating', 0), m.get('vote_count', 0)), reverse=True)
    return movies[:limit]


def search_movies_direct(title, limit=6):
    title = str(title or '').strip()
    if not title:
        return []
    if tmdb_enabled():
        data = tmdb_get('/search/movie', {'query': title, 'include_adult': 'false', 'page': 1}) or {}
        return [normalize_tmdb(x) for x in data.get('results', [])[:limit]]
    items = []
    for movie in LOCAL_MOVIES:
        score = similarity(title, movie['title'])
        if title.lower() in movie['title'].lower() or score >= 0.35:
            copy_movie = dict(movie)
            copy_movie['_match_score'] = score
            items.append(copy_movie)
    items.sort(key=lambda m: (m.get('_match_score', 0), m.get('rating', 0), m.get('vote_count', 0)), reverse=True)
    return items[:limit]


def resolve_movie_candidates(title, limit=5):
    candidates = search_movies_direct(title, limit=max(limit, 6))
    if not candidates:
        return None, []
    exact = next((m for m in candidates if m['title'].lower() == title.lower()), None)
    if exact:
        return exact, candidates[:limit]
    scored = sorted([(similarity(title, m['title']), m) for m in candidates], key=lambda x: x[0], reverse=True)
    best_score, best_movie = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    if best_score >= 0.86 or best_score - second_score >= 0.18:
        return best_movie, candidates[:limit]
    close = [m for score, m in scored if score >= 0.45 or title.lower() in m['title'].lower()]
    if len(close) > 1:
        return None, close[:limit]
    return best_movie, candidates[:limit]


def discover_movies_for_genre(genre_text, limit=5):
    genre_text = str(genre_text or '').strip().lower()
    if not genre_text:
        return []
    genre_text = GENRE_ALIASES.get(genre_text, genre_text.title())
    genre_name = next((name for name in GENRE_ID_MAP if name.lower() == genre_text.lower()), '')
    if not genre_name:
        return []
    genre_id = GENRE_ID_MAP[genre_name]
    if tmdb_enabled():
        data = tmdb_get('/discover/movie', {'with_genres': genre_id, 'sort_by': 'popularity.desc', 'page': 1, 'include_adult': 'false'}) or {}
        return [normalize_tmdb(x) for x in data.get('results', [])[:limit]]
    movies = [m for m in LOCAL_MOVIES if genre_id in m.get('genre_ids', [])]
    movies.sort(key=lambda m: (m.get('rating', 0), m.get('vote_count', 0)), reverse=True)
    return movies[:limit]


def movie_directors(movie):
    if movie['source'] != 'tmdb' or not tmdb_enabled():
        return []
    credits = tmdb_get(f"/movie/{movie['id']}/credits") or {}
    return [person.get('name') for person in credits.get('crew', []) if person.get('job') == 'Director']


def movie_cast(movie, limit=5):
    if movie['source'] != 'tmdb' or not tmdb_enabled():
        return []
    credits = tmdb_get(f"/movie/{movie['id']}/credits") or {}
    return [person.get('name') for person in credits.get('cast', [])[:limit] if person.get('name')]


def is_greeting(message):
    text = str(message or '').strip().lower()
    return bool(re.fullmatch(r'(hi|hello|hey|hola|yo|good morning|good afternoon|good evening|help)', text))


def likely_new_topic(message):
    text = str(message or '').strip().lower()
    if is_greeting(text):
        return True
    triggers = ['who ', 'what ', "what's", 'show me', 'movies of', 'films of', 'tell me', 'director', 'cast', 'song', '?']
    if any(token in text for token in triggers):
        return True
    if len(text.split()) >= 2 and not re.fullmatch(r'[1-5]|(19|20)\d{2}', text):
        return True
    return False


def classify_chat_message(message):
    text = str(message or '').strip()
    lowered = text.lower()
    heuristic = {
        'intent': 'unknown',
        'movie_title': '',
        'song_title': '',
        'genre': '',
        'fact_type': '',
        'person_names': [],
        'person_name': '',
    }
    if is_greeting(text):
        heuristic['intent'] = 'greeting'
        return heuristic
    if re.search(r'\b(show me|find|give me)\s+.*\bmovies?\s+of\b', lowered) or lowered.startswith('movies of ') or lowered.startswith('films of '):
        heuristic['intent'] = 'person_lookup'
        cleaned = re.sub(r'\b(show me|find|give me)\s+', '', text, flags=re.IGNORECASE)
        cleaned = re.sub(r'\bmovies?\s+of\b|\bfilms?\s+of\b', '', cleaned, flags=re.IGNORECASE).strip(' ?')
        heuristic['person_name'] = cleaned
        return heuristic
    if re.search(r'\bshow me .* movies\b', lowered):
        heuristic['intent'] = 'genre_discover'
        heuristic['genre'] = re.sub(r'.*show me\s+', '', lowered).replace(' movies', '').strip()
        return heuristic
    if any(phrase in lowered for phrase in ['who directed', 'director of']):
        heuristic['intent'] = 'movie_fact'
        heuristic['fact_type'] = 'director'
        title = re.sub(r'who directed|director of', '', lowered, flags=re.IGNORECASE).replace('?', '').strip()
        heuristic['movie_title'] = title.title()
        return heuristic
    if any(phrase in lowered for phrase in ['who starred in', 'cast of', 'who is in']):
        heuristic['intent'] = 'movie_fact'
        heuristic['fact_type'] = 'cast'
        title = lowered
        for phrase in ['who starred in', 'cast of', 'who is in']:
            title = title.replace(phrase, '')
        heuristic['movie_title'] = title.replace('?', '').strip().title()
        return heuristic
    if any(phrase in lowered for phrase in ['runtime of', 'how long is']):
        heuristic['intent'] = 'movie_fact'
        heuristic['fact_type'] = 'runtime'
        title = lowered
        for phrase in ['runtime of', 'how long is']:
            title = title.replace(phrase, '')
        heuristic['movie_title'] = title.replace('?', '').strip().title()
        return heuristic
    if any(phrase in lowered for phrase in ['release year of', 'when was', 'what year was']):
        heuristic['intent'] = 'movie_fact'
        heuristic['fact_type'] = 'release_year'
        title = lowered
        for phrase in ['release year of', 'when was', 'what year was']:
            title = title.replace(phrase, '')
        heuristic['movie_title'] = title.replace('released', '').replace('?', '').strip().title()
        return heuristic
    if any(phrase in lowered for phrase in ["tell me about", "what is ", "what's "]):
        heuristic['intent'] = 'movie_fact'
        heuristic['fact_type'] = 'overview'
        title = text
        for phrase in ['Tell me about', 'tell me about', 'What is', "What's", 'what is', "what's"]:
            title = title.replace(phrase, '')
        heuristic['movie_title'] = title.replace('?', '').strip()
        return heuristic
    names = [part.strip() for part in re.split(r'\band\b', text, flags=re.IGNORECASE)]
    if len(names) == 2 and all(len(name.split()) >= 2 for name in names):
        heuristic['intent'] = 'pair_people'
        heuristic['person_names'] = names
        return heuristic
    if gemini_enabled():
        prompt = (
            'Classify this movie chatbot message. Return strict JSON only with keys '
            '{"intent":"unknown|greeting|pair_people|song_lookup|movie_fact|genre_discover|movie_lookup|person_lookup","movie_title":"","song_title":"","genre":"","fact_type":"director|cast|overview|release_year|runtime|movie_lookup","person_names":["",""],"person_name":""}. '
            'Use pair_people when the message is mainly two actor names. Use person_lookup for one actor or actress name. '
            'Use song_lookup for a song title. Use genre_discover for requests like show me sci-fi movies. '
            'Use movie_fact for questions about one movie. Keep the output grounded and concise. '
            f'Message: {json.dumps(text, ensure_ascii=False)}'
        )
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}'
        payload = {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0}}
        try:
            r = requests.post(url, json=payload, timeout=18)
            r.raise_for_status()
            data = r.json()
            raw = data['candidates'][0]['content']['parts'][0]['text']
            parsed = safe_json_parse(raw)
            if isinstance(parsed, dict) and parsed.get('intent'):
                parsed.setdefault('movie_title', '')
                parsed.setdefault('song_title', '')
                parsed.setdefault('genre', '')
                parsed.setdefault('fact_type', '')
                parsed.setdefault('person_names', [])
                parsed.setdefault('person_name', '')
                return parsed
        except Exception:
            pass
    return heuristic


def song_to_movie_candidates(song_title, limit=5):
    song_title = str(song_title or '').strip()
    if not song_title or not gemini_enabled():
        return []
    prompt = (
        'You are helping a movie chatbot. The user gave a song title. '
        'Identify likely movie titles that the song is strongly associated with. '
        'Return strict JSON only in this format: '
        '{"movies":[{"title":"Titanic","reason":"..."}]}. '
        'Prefer actual movies, not albums or artists. Return at most 5 movie titles. '
        'Do not guess wildly. '
        f'Song title: {json.dumps(song_title, ensure_ascii=False)}'
    )
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}'
    payload = {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0}}
    try:
        r = requests.post(url, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        raw = data['candidates'][0]['content']['parts'][0]['text']
        parsed = safe_json_parse(raw) or {}
    except Exception:
        return []
    verified = []
    for item in parsed.get('movies', [])[:limit]:
        movie_title = str(item.get('title', '')).strip()
        movie, _ = resolve_movie_candidates(movie_title, limit=5)
        if movie:
            verified.append(movie)
    return dedupe_movies(verified)[:limit]


def chat_cards_payload(movies):
    cards = []
    for movie in movies:
        cards.append({
            'title': movie['title'],
            'year': movie.get('year', '—'),
            'poster': movie.get('poster', ''),
            'language': movie.get('language', ''),
            'genres': movie.get('genres', []),
            'url': url_for('movie_detail', source=movie['source'], movie_id=movie['id']),
        })
    return cards


def pending_choice_from_message(message, pending):
    text = str(message or '').strip().lower()
    candidates = pending.get('candidates', [])
    if not candidates:
        return None
    number_match = re.search(r'\b([1-5])\b', text)
    if number_match:
        index = int(number_match.group(1)) - 1
        if 0 <= index < len(candidates):
            return candidates[index]
    for candidate in candidates:
        if str(candidate.get('year', '')).lower() and str(candidate.get('year', '')).lower() in text:
            return candidate
        if candidate.get('title', '').lower() in text:
            return candidate
    return None


def chatbot_answer_for_movie_fact(movie, fact_type):
    fact_type = fact_type or 'overview'
    if fact_type == 'director':
        directors = movie_directors(movie)
        if directors:
            return f"{movie['title']} was directed by {', '.join(directors)}.", chat_cards_payload([movie])
        return f"I could not confirm the director for {movie['title']} right now.", chat_cards_payload([movie])
    if fact_type == 'cast':
        cast = movie_cast(movie)
        if cast:
            return f"Main cast for {movie['title']}: {', '.join(cast)}.", chat_cards_payload([movie])
        return f"I could not load the cast for {movie['title']} right now.", chat_cards_payload([movie])
    if fact_type == 'release_year':
        return f"{movie['title']} was released in {movie.get('year', 'unknown year')}.", chat_cards_payload([movie])
    if fact_type == 'runtime':
        runtime = movie.get('runtime')
        if runtime:
            return f"{movie['title']} runs for about {runtime} minutes.", chat_cards_payload([movie])
        return f"I could not confirm the runtime for {movie['title']} right now.", chat_cards_payload([movie])
    return f"{movie['title']}: {movie.get('overview') or 'I could not load the overview right now.'}", chat_cards_payload([movie])


def chatbot_answer_for_person(person):
    name = person.get('name', 'this person')
    department = person.get('known_for_department') or 'film'
    movies = person_known_movies(person, limit=5)
    if movies:
        return f"I found {name}. Here are some movies connected to {name} from TMDB.", chat_cards_payload(movies)
    return f"I found {name}, but I could not load movie links for {name} right now.", []


def build_chatbot_reply(message):
    if is_greeting(message):
        session.pop('chatbot_pending', None)
        return 'Hello! Ask me about a movie, actor, song, director, or genre, and I will show real movie matches you can open.', [], False

    pending = session.get('chatbot_pending')
    preclassified = None
    if pending:
        chosen = pending_choice_from_message(message, pending)
        if chosen:
            session.pop('chatbot_pending', None)
            if pending.get('mode') == 'movie_fact':
                return chatbot_answer_for_movie_fact(chosen, pending.get('fact_type')) + (False,)
            return f"Here is the movie I found: {chosen['title']}.", chat_cards_payload([chosen]), False
        if likely_new_topic(message):
            session.pop('chatbot_pending', None)
            preclassified = classify_chat_message(message)
        else:
            return pending.get('ask', 'I found a few matches. Please tell me which one you mean by title or year.'), chat_cards_payload(pending.get('candidates', [])), True

    info = preclassified or classify_chat_message(message)
    intent = info.get('intent', 'unknown')

    if intent == 'greeting':
        return 'Hello! Ask me about a movie, actor, song, director, or genre, and I will show real movie matches you can open.', [], False

    if intent == 'pair_people' and len(info.get('person_names', [])) >= 2:
        people = info['person_names'][:2]
        movies = person_common_movies(people[0], people[1], limit=5)
        if not movies:
            return f"I could not confidently find a movie that links {people[0]} and {people[1]} right now.", [], False
        if len(movies) == 1:
            return f"I found {movies[0]['title']} for {people[0]} and {people[1]}.", chat_cards_payload(movies[:1]), False
        session['chatbot_pending'] = {
            'mode': 'movie_choice',
            'candidates': movies[:5],
            'ask': f"I found several movies connected to {people[0]} and {people[1]}. Please choose one by title or year.",
        }
        return f"I found several movies connected to {people[0]} and {people[1]}. Please choose one by title or year.", chat_cards_payload(movies[:5]), True

    if intent == 'song_lookup':
        song_title = info.get('song_title') or str(message).strip()
        movies = song_to_movie_candidates(song_title, limit=5)
        if not movies:
            return f"I could not confidently match the song '{song_title}' to a movie right now.", [], False
        if len(movies) == 1:
            return f"'{song_title}' is strongly associated with {movies[0]['title']}.", chat_cards_payload(movies[:1]), False
        session['chatbot_pending'] = {
            'mode': 'movie_choice',
            'candidates': movies[:5],
            'ask': f"I found multiple movie matches for '{song_title}'. Please choose one by title or year.",
        }
        return f"I found multiple movie matches for '{song_title}'. Please choose one by title or year.", chat_cards_payload(movies[:5]), True

    if intent == 'genre_discover':
        genre = info.get('genre') or str(message).lower().replace('show me', '').replace('movies', '').strip()
        movies = discover_movies_for_genre(genre, limit=5)
        if movies:
            nice_genre = GENRE_ALIASES.get(genre.lower(), genre).title()
            return f"Here are some {nice_genre} movies you can open right away.", chat_cards_payload(movies), False
        return f"I could not find movies for the genre '{genre}' right now.", [], False

    if intent in {'movie_fact', 'movie_lookup'}:
        movie_title = info.get('movie_title') or str(message).strip()
        fact_type = info.get('fact_type') or 'overview'
        movie, candidates = resolve_movie_candidates(movie_title, limit=5)
        if movie is not None:
            return chatbot_answer_for_movie_fact(movie, fact_type) + (False,)
        if candidates:
            session['chatbot_pending'] = {
                'mode': 'movie_fact',
                'fact_type': fact_type,
                'candidates': candidates,
                'ask': f"I found multiple movies for '{movie_title}'. Please choose one by title or year.",
            }
            return f"I found multiple movies for '{movie_title}'. Please choose one by title or year.", chat_cards_payload(candidates), True
        return f"I could not confidently find a movie for '{movie_title}'. Please add a year or another clue.", [], False

    if intent == 'person_lookup':
        person_name = info.get('person_name') or str(message).strip()
        person, candidates = resolve_person_candidate(person_name, limit=5)
        if person is not None:
            reply, cards = chatbot_answer_for_person(person)
            return reply, cards, False
        if candidates:
            names = ', '.join(item.get('name', '') for item in candidates[:3] if item.get('name'))
            return f"I found several people for '{person_name}': {names}. Add one movie title or more detail so I can narrow it down.", [], False
        return f"I could not confidently find a person named '{person_name}' right now.", [], False

    movie, candidates = resolve_movie_candidates(str(message).strip(), limit=5)
    if movie is not None:
        return f"I found {movie['title']}. You can open its detail page below.", chat_cards_payload([movie]), False
    if candidates:
        session['chatbot_pending'] = {
            'mode': 'movie_choice',
            'candidates': candidates,
            'ask': 'I found several possible movies. Please choose one by title or year.',
        }
        return 'I found several possible movies. Please choose one by title or year.', chat_cards_payload(candidates), True

    person, person_candidates = resolve_person_candidate(str(message).strip(), limit=5)
    if person is not None:
        reply, cards = chatbot_answer_for_person(person)
        return reply, cards, False
    if person_candidates:
        names = ', '.join(item.get('name', '') for item in person_candidates[:3] if item.get('name'))
        return f"I found several people with similar names: {names}. Add one movie title or more detail so I can narrow it down.", [], False

    return "I’m sorry, I don’t know that yet. Try a movie title, an actor name, two actor names, a song name, or a genre like sci-fi.", [], False

def collect_candidates(user, filters=None):
    filters = filters or {}
    if tmdb_enabled():
        out = []
        for title in parse_csv_field(user['favorite_movies'])[:3]:
            data = tmdb_get('/search/movie', {'query': title, 'include_adult': 'false', 'page': 1}) or {}
            out.extend(normalize_tmdb(x) for x in data.get('results', [])[:6])
        for genre in parse_csv_field(user['preferred_genres'])[:3]:
            gid = GENRE_ID_MAP.get(genre.strip())
            if gid:
                data = tmdb_get('/discover/movie', {'with_genres': gid, 'sort_by': 'vote_average.desc', 'vote_count.gte': 120, 'page': 1}) or {}
                out.extend(normalize_tmdb(x) for x in data.get('results', [])[:8])
        if filters.get('genre'):
            data = tmdb_get('/discover/movie', {'with_genres': filters['genre'], 'sort_by': 'popularity.desc', 'page': 1}) or {}
            out.extend(normalize_tmdb(x) for x in data.get('results', [])[:8])
        popular = tmdb_get('/movie/popular', {'page': 1}) or {}
        out.extend(normalize_tmdb(x) for x in popular.get('results', [])[:10])
        top = tmdb_get('/movie/top_rated', {'page': 1}) or {}
        out.extend(normalize_tmdb(x) for x in top.get('results', [])[:10])
        return dedupe_movies(out)
    return list(LOCAL_MOVIES)


def personalized_for_user(user, filters=None, limit=5):
    if user is None:
        return [], 'guest'
    candidates = collect_candidates(user, filters)
    local = local_recommend(user, candidates, filters, limit=20)
    if gemini_enabled():
        live = gemini_rerank(user, local[:20], filters, limit=limit)
        if live:
            return live, 'gemini'
    return local[:limit], 'local'


@app.route('/')
def index():
    sections = home_sections()
    user = current_user()
    for_you, engine = personalized_for_user(user, limit=5)
    return render_template('index.html', sections=sections, for_you=for_you, engine=engine)


@app.route('/search')
def search_page():
    q = request.args.get('q', '').strip()
    genre = request.args.get('genre', '').strip()
    year = request.args.get('year', '').strip()
    language = request.args.get('language', '').strip().lower()
    rating = request.args.get('rating', '0').strip() or '0'
    sort = request.args.get('sort', 'popularity.desc').strip() or 'popularity.desc'
    page = request.args.get('page', '1').strip() or '1'
    catalog = search_catalog(q, genre, year, language, rating, sort, page)
    filters = {
        'query': q,
        'genre': genre,
        'genre_name': GENRE_NAME_MAP.get(int(genre), '') if genre.isdigit() else '',
        'year': year,
        'language': language,
        'rating': rating,
        'sort': sort,
    }
    ai_recommendations, ai_mode = personalized_for_user(current_user(), filters=filters, limit=5)
    return render_template('results.html', recommendations=catalog['results'], filters=filters, pagination=catalog, ai_recommendations=ai_recommendations, ai_mode=ai_mode)


@app.route('/recommend', methods=['POST'])
def recommend_redirect():
    return redirect(url_for(
        'search_page',
        q=request.form.get('q', ''),
        genre=request.form.get('genre', ''),
        year=request.form.get('year', ''),
        language=request.form.get('language', ''),
        rating=request.form.get('rating', '0'),
        sort=request.form.get('sort', 'popularity.desc'),
    ))


@app.route('/chatbot', methods=['POST'])
def chatbot_api():
    payload = request.get_json(silent=True) or {}
    message = str(payload.get('message', '')).strip()
    if not message:
        return jsonify({'reply': 'Please type a movie question for me.', 'cards': [], 'needs_followup': False})
    reply, cards, needs_followup = build_chatbot_reply(message)
    return jsonify({'reply': reply, 'cards': cards, 'needs_followup': needs_followup})


@app.route('/chatbot/reset', methods=['POST'])
def chatbot_reset():
    session.pop('chatbot_pending', None)
    return jsonify({'ok': True})


@app.route('/movie/<string:source>/<int:movie_id>')
def movie_detail(source, movie_id):
    if source == 'tmdb' and tmdb_enabled():
        data = tmdb_get(f'/movie/{movie_id}', {'append_to_response': 'credits,similar,videos'})
        if data:
            movie = normalize_tmdb(data)
            movie['runtime'] = data.get('runtime')
            movie['tagline'] = data.get('tagline', '')
            cast = (data.get('credits') or {}).get('cast', [])[:8]
            similar = [normalize_tmdb(x) for x in (data.get('similar') or {}).get('results', [])[:8]]
            trailer = None
            for v in (data.get('videos') or {}).get('results', []):
                if v.get('site') == 'YouTube' and v.get('type') == 'Trailer':
                    trailer = f"https://www.youtube.com/watch?v={v.get('key')}"
                    break
            return render_template('movie_detail.html', movie=movie, cast=cast, similar_movies=similar, trailer=trailer)
    movie = LOCAL_MOVIE_MAP.get(movie_id)
    if not movie:
        flash('Movie details could not be loaded.')
        return redirect(url_for('index'))
    similar = [m for m in LOCAL_MOVIES if m['id'] != movie_id and set(m['genre_ids']) & set(movie['genre_ids'])][:8]
    return render_template('movie_detail.html', movie=movie, cast=[], similar_movies=similar, trailer=None)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not username or not password:
            flash('Please enter both username and password.')
            return redirect(url_for('register'))
        conn = db()
        try:
            conn.execute(
                'INSERT INTO users (username, password, role, created_at) VALUES (?, ?, ?, ?)',
                (username, generate_password_hash(password), 'user', datetime.utcnow().isoformat(timespec='seconds')),
            )
            conn.commit()
            flash('Registration successful. Please log in.')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username already exists. Please choose another one.')
            return redirect(url_for('register'))
        finally:
            conn.close()
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        role = request.form.get('role', 'user').strip().lower()
        conn = db()
        user = conn.execute('SELECT * FROM users WHERE username=? AND role=?', (username, role)).fetchone()
        now = datetime.utcnow().isoformat(timespec='seconds')
        if user and verify_password(user['password'], password):
            session['user_id'] = user['id']
            conn.execute('UPDATE users SET last_login=? WHERE id=?', (now, user['id']))
            conn.execute('INSERT INTO login_activity (username, role, status, login_time) VALUES (?, ?, ?, ?)', (username, role, 'Success', now))
            conn.commit()
            conn.close()
            flash(f"Welcome, {user['username']}! You are logged in as {user['role']}.")
            return redirect(url_for('index'))
        conn.execute('INSERT INTO login_activity (username, role, status, login_time) VALUES (?, ?, ?, ?)', (username or 'Unknown', role, 'Failed', now))
        conn.commit()
        conn.close()
        flash('Invalid login details. Please try again.')
        return redirect(url_for('login'))
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.')
    return redirect(url_for('index'))


@app.route('/preferences', methods=['GET', 'POST'])
def preferences():
    user = current_user()
    if user is None:
        flash('Please log in to save your movie preferences.')
        return redirect(url_for('login'))
    if request.method == 'POST':
        fav = request.form.get('favorite_movies', '').strip()
        genres = request.form.get('preferred_genres', '').strip()
        langs = request.form.get('preferred_languages', '').strip()
        moods = request.form.get('preferred_moods', '').strip()
        conn = db()
        conn.execute('UPDATE users SET favorite_movies=?, preferred_genres=?, preferred_languages=?, preferred_moods=? WHERE id=?', (fav, genres, langs, moods, user['id']))
        conn.commit()
        conn.close()
        flash('Your taste profile has been saved. Fresh recommendations are ready below.')
        return redirect(url_for('preferences'))
    user = current_user()
    recs, engine = personalized_for_user(user, limit=5)
    return render_template('preferences.html', user=user, preference_recommendations=recs, engine=engine)


@app.route('/admin-dashboard')
@admin_required
def admin_dashboard():
    conn = db()
    users = conn.execute('SELECT id, username, role, created_at, last_login, favorite_movies, preferred_genres, preferred_languages, preferred_moods FROM users ORDER BY id DESC').fetchall()
    logs = conn.execute('SELECT username, role, status, login_time FROM login_activity ORDER BY id DESC LIMIT 30').fetchall()
    stats = {
        'total_users': conn.execute("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0],
        'total_admins': conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0],
        'successful_logins': conn.execute("SELECT COUNT(*) FROM login_activity WHERE status='Success'").fetchone()[0],
        'failed_logins': conn.execute("SELECT COUNT(*) FROM login_activity WHERE status='Failed'").fetchone()[0],
    }
    conn.close()
    return render_template('admin_dashboard.html', users=users, login_logs=logs, stats=stats)


if __name__ == '__main__':
    app.run(debug=True)
