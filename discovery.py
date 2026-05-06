import csv
import json
import logging
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path

from thefuzz import fuzz, process
from config import DATA_DIR, LASTFM_API_KEY, TIDAL_CSV_PATH

log = logging.getLogger(__name__)

CACHE_DB_PATH = DATA_DIR / "lastfm_cache.db"

class DiscoveryEngine:
    def __init__(self, top_similar: int = 20, discovery_threshold: int = 5):
        """
        Initialize the discovery engine.
        :param top_similar: number of similar artists to fetch from Last.fm
        :param discovery_threshold: minimum overlap score to recommend an event
        """
        self.top_similar = top_similar
        self.discovery_threshold = discovery_threshold
        self.taste_profile = self._load_taste_profile()
        self._init_cache()

    def _load_taste_profile(self) -> dict[str, int]:
        """Reads the TIDAL CSV and calculates artist weights based on track counts."""
        profile = {}
        try:
            with open(TIDAL_CSV_PATH, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    artist = row.get("Artist name", "").strip().lower()
                    if artist:
                        profile[artist] = profile.get(artist, 0) + 1
            log.info("Loaded Taste Profile: %d unique artists from TIDAL.", len(profile))
        except Exception as e:
            log.warning("Could not load TIDAL CSV from %s: %s", TIDAL_CSV_PATH, e)
        return profile

    def _init_cache(self):
        """Initialize the local SQLite cache for Last.fm API results."""
        with sqlite3.connect(CACHE_DB_PATH) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS similar_artists (
                    artist TEXT PRIMARY KEY,
                    similar_json TEXT
                )
            ''')

    def _get_similar_from_lastfm(self, artist: str) -> list[str]:
        """Fetch similar artists from Last.fm (or cache)."""
        if not LASTFM_API_KEY:
            # Silently fail if API key is not configured
            return []

        clean_artist = artist.lower()

        with sqlite3.connect(CACHE_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT similar_json FROM similar_artists WHERE artist = ?", (clean_artist,))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])

        url = (f"http://ws.audioscrobbler.com/2.0/?method=artist.getsimilar"
               f"&artist={urllib.parse.quote(artist)}&api_key={LASTFM_API_KEY}"
               f"&format=json&limit={self.top_similar}")
        similar = []
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'AmsterdamEventAggregator/1.0'})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode('utf-8'))
                if 'similarartists' in data and 'artist' in data['similarartists']:
                    similar = [item['name'].lower() for item in data['similarartists']['artist']]
        except Exception as e:
            log.warning("Last.fm API fetch failed for '%s': %s", artist, e)

        # Cache the result (even if empty, to prevent spamming the API on unmatchable artists)
        with sqlite3.connect(CACHE_DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO similar_artists (artist, similar_json) VALUES (?, ?)",
                         (clean_artist, json.dumps(similar)))

        return similar

    def evaluate_artist(self, event_artist: str) -> dict:
        """
        Evaluates a single artist against the taste profile.
        Returns a dict indicating if it's a match, the match type, and the reason.
        """
        clean_artist = str(event_artist).strip().lower()
        if not clean_artist or not self.taste_profile:
            return {"match": False}

        # Tier 1: Exact Match
        if clean_artist in self.taste_profile:
            return {
                "match": True,
                "type": "Library Match",
                "score": self.taste_profile[clean_artist],
                "reason": "In your TIDAL library"
            }

        # Tier 2: Fuzzy Match
        library_artists = list(self.taste_profile.keys())
        # Use token_sort_ratio (stricter than token_set_ratio) and a high threshold of 90 
        # to prevent false positives where short words match long unrelated phrases.
        best_match, score = process.extractOne(clean_artist, library_artists, scorer=fuzz.token_sort_ratio)
        if score >= 90:
            return {
                "match": True,
                "type": "Library Match",
                "score": self.taste_profile[best_match],
                "reason": f"Fuzzy match for '{best_match.title()}'"
            }

        # Tier 3: Semantic Discovery Match
        similar_artists = self._get_similar_from_lastfm(clean_artist)
        if similar_artists:
            overlapping = []
            discovery_score = 0
            for sim_artist in similar_artists:
                if sim_artist in self.taste_profile:
                    weight = self.taste_profile[sim_artist]
                    overlapping.append((sim_artist.title(), weight))
                    discovery_score += weight

            if discovery_score >= self.discovery_threshold:
                overlapping.sort(key=lambda x: x[1], reverse=True)
                top_reasons = [item[0] for item in overlapping[:3]]
                return {
                    "match": True,
                    "type": "Discovery",
                    "score": discovery_score,
                    "reason": f"Similar to {', '.join(top_reasons)}"
                }

        return {"match": False}

    def evaluate_event(self, artists: list[str]) -> dict:
        """
        Evaluate a list of artists for an event. Returns the best match found.
        Library matches override Discovery matches.
        """
        best_discovery = {"match": False}
        
        for artist in artists:
            res = self.evaluate_artist(artist)
            if res["match"]:
                if res["type"] == "Library Match":
                    # Library matches are top priority, return immediately
                    return res
                else:
                    # Keep track of the best discovery match
                    if not best_discovery["match"] or res["score"] > best_discovery["score"]:
                        best_discovery = res
                        
        return best_discovery
