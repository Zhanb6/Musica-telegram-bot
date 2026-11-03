"""Telegram bot that returns a YouTube video as MP3 or MP4."""

import asyncio
import difflib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unicodedata
import zipfile
from contextlib import AsyncExitStack, ExitStack, closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from dotenv import load_dotenv
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TIT2, TPE1
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telethon import TelegramClient, events, types
from telethon.errors import RPCError
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


LOG = logging.getLogger(__name__)
HISTORY_DB = Path(os.getenv("TELEGA_DB_PATH") or Path(__file__).with_name("downloads.sqlite3"))
MAX_FILE_BYTES = 50_000_000  # Telegram Bot API upload limit
MAX_BATCH = 10
SEARCH_RESULT_LIMIT = 10
SEARCH_UI_TIMEOUT = 120
PLAYLIST_LIMIT = 30
AUTO_PLAYLIST_SCAN_LIMIT = 150
AUTO_PLAYLIST_DOWNLOAD_LIMIT = 30
PLAYLIST_PAGE_SIZE = 8
MAX_PLAYLIST_SELECTION = 150
ARTIST_TOPIC_THRESHOLD = 5
MISC_TOPIC_NAME = "NoName"
FAVORITES_TOPIC_KEY = "__favorites__"
FAVORITES_TOPIC_NAME = "Избранные"
TRANSIENT_REPLY_SECONDS = 20
ZIP_SUCCESS_STATUS_SECONDS = 20
ZIP_ERROR_STATUS_SECONDS = 120
SHARE_SEND_INTERVAL = 1.0
ZIP_PART_TARGET_BYTES = 45_000_000
ZIP_FETCH_LIMIT_BYTES = 20_000_000
SHARE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32}$")
QUALITY_OPTIONS = {"mp3": ("128", "192", "320"), "mp4": ("360", "480", "720")}
DEFAULT_QUALITY = {"mp3": "192", "mp4": "480"}
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAYLIST_ID = re.compile(r"^[A-Za-z0-9_-]{2,100}$")
URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
VIDEO_SUFFIX = re.compile(
    r"\s*[\[(](?:official\s+)?(?:(?:music|lyrics?)\s+)?(?:video|audio|lyrics?|visualizer)(?:\s+\d{4})?[\])]\s*$",
    re.IGNORECASE,
)


def normalize_youtube_url(value: str) -> str | None:
    """Accept a single YouTube video URL and strip playlist/tracking parameters."""
    try:
        parsed = urlsplit(value.strip())
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return None
        if parsed.port not in {None, 80, 443}:
            return None
    except ValueError:
        return None

    segments = [part for part in parsed.path.split("/") if part]
    video_id = None
    if hostname in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif len(segments) == 2 and segments[0] in {"shorts", "live", "embed"}:
            video_id = segments[1]
    elif hostname == "youtu.be" and len(segments) == 1:
        video_id = segments[0]

    if video_id and VIDEO_ID.fullmatch(video_id):
        return f"https://www.youtube.com/watch?v={video_id}"
    return None


def normalize_playlist_url(value: str) -> str | None:
    """Keep only validated YouTube watch/playlist identifiers."""
    try:
        parsed = urlsplit(value.strip())
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return None
        if parsed.port not in {None, 80, 443}:
            return None
    except ValueError:
        return None
    if hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        return None
    query = parse_qs(parsed.query)
    playlist_id = query.get("list", [None])[0]
    if not playlist_id or not PLAYLIST_ID.fullmatch(playlist_id):
        return None
    if parsed.path == "/playlist":
        return f"https://www.youtube.com/playlist?list={playlist_id}"
    if parsed.path == "/watch":
        video_id = query.get("v", [None])[0]
        if video_id and VIDEO_ID.fullmatch(video_id):
            url = f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}"
            if playlist_id.startswith("RD"):
                url += "&start_radio=1"
            return url
    return None


def list_playlist(url: str, start: int = 1) -> tuple[str, list[tuple[str, str, str]], bool]:
    """Fetch one page and peek at the next item without loading an endless Mix."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playliststart": start,
        "playlistend": start + PLAYLIST_LIMIT,
        "noplaylist": False,
        "skip_download": True,
    }
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
    if not info or info.get("_type") != "playlist":
        raise ValueError("Не удалось получить список треков.")
    entries = []
    seen = set()
    raw_entries = info.get("entries") or []
    for entry in raw_entries[:PLAYLIST_LIMIT]:
        if not entry:
            continue
        video_id = entry.get("id")
        if isinstance(video_id, str) and VIDEO_ID.fullmatch(video_id) and video_id not in seen:
            seen.add(video_id)
            artist, track = music_metadata(entry)
            entries.append((
                f"https://www.youtube.com/watch?v={video_id}",
                entry.get("title") or video_id,
                song_key(artist, track),
            ))
    if not entries and start == 1:
        raise ValueError("В этом миксе пока нет доступных треков.")
    return (info.get("title") or "YouTube Mix")[:100], entries, len(raw_entries) > PLAYLIST_LIMIT


def search_youtube(query: str) -> list[tuple[str, str, str, int | None]]:
    """Get video search results without downloading their media."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "noplaylist": False,
    }
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(f"ytsearch{SEARCH_RESULT_LIMIT}:{query}", download=False)
    results = []
    seen = set()
    for entry in (info or {}).get("entries") or []:
        if not entry or entry.get("is_live") or entry.get("live_status") in {"is_live", "is_upcoming"}:
            continue
        video_id = entry.get("id")
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id) or video_id in seen:
            continue
        seen.add(video_id)
        artist, title = music_metadata(entry)
        duration = entry.get("duration")
        results.append((
            f"https://www.youtube.com/watch?v={video_id}",
            str(entry.get("title") or title).replace("\n", " ").strip()[:150],
            song_key(artist, title),
            int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None,
        ))
    return results


def extract_urls(text: str) -> tuple[list[str], int]:
    """Read all distinct YouTube video links from one message."""
    urls = []
    invalid = 0
    for raw in URL.findall(text):
        url = normalize_youtube_url(raw.rstrip(".,!?;:)]}"))
        if url:
            if url not in urls:
                urls.append(url)
        else:
            invalid += 1
    return urls, invalid


def music_metadata(info: dict) -> tuple[str | None, str]:
    """Prefer music metadata, then infer artist and track from a video title."""
    original_title = (info.get("title") or "YouTube").strip()
    title = VIDEO_SUFFIX.sub("", original_title).strip() or original_title
    artists = info.get("artists")
    artist = info.get("artist") or (", ".join(artists) if isinstance(artists, (list, tuple)) and artists else None)
    artist = artist or info.get("creator")
    track = info.get("track")
    if " - " in title:
        from_title, from_title_track = title.split(" - ", 1)
        title_first = bool(re.search(
            r"\([^)]*\b(?:feat\.?|ft\.?|featuring)\s+[^)]*\)", from_title, re.IGNORECASE,
        ) and (
            (artist and artist.casefold() == from_title_track.strip().casefold())
            or (not artist and (
                not track or str(track).split("(", 1)[0].strip().casefold()
                == from_title.split("(", 1)[0].strip().casefold()
            ))
        ))
        artist = artist or (from_title_track.strip() if title_first else from_title.strip())
        track = track or (from_title.strip() if title_first else from_title_track.strip())
    track = str(track or title).strip()
    if track != title:
        feature = re.search(r"\b(?:feat\.?|ft\.?|featuring)\s+[^\])]+", title, re.IGNORECASE)
        if feature:
            current = re.search(r"\b(?:feat\.?|ft\.?|featuring)\s+[^\])]+", track, re.IGNORECASE)
            if current:
                if set(credited_artists("", feature.group(0))) - set(credited_artists("", current.group(0))):
                    track = track[:current.start()] + feature.group(0).strip() + track[current.end():]
            else:
                track = f"{track} ({feature.group(0).strip()})"
    if not artist:
        artist = info.get("uploader") or info.get("channel")
        if artist and artist.endswith(" - Topic"):
            artist = artist[:-8]
    return (str(artist).strip()[:64] if artist else None), track[:128]


def song_key(artist: str | None, title: str) -> str:
    """Match the main artist and song despite credits or video packaging."""
    if not artist:
        return ""
    artist = unicodedata.normalize("NFKC", primary_artist(artist)).casefold()
    title = unicodedata.normalize("NFKC", title).casefold()
    title = re.sub(
        r"\s*[\[(](?:official\s+)?(?:music\s+)?(?:video|audio|lyric(?:s|\s+video)?|visualizer|\d+k)[^\])]*[\])]\s*$",
        "", title, flags=re.IGNORECASE,
    )
    title = re.sub(r"\s+(?:official\s+)?(?:music\s+)?(?:video|audio|lyrics?|visualizer)\s*$", "", title)
    title = re.sub(r"\s*(?:\(|\[)?\s*(?:feat\.?|ft\.?|featuring)\s+[^\])]+[\])]?$", "", title)
    artist_part = re.sub(r"[^\w]+", " ", artist).strip()
    title_part = re.sub(r"[^\w]+", " ", title).strip()
    return f"{artist_part}|{title_part}" if artist_part and title_part else ""


def primary_artist(artist: str) -> str:
    """Return the lead credit, merging known names for the same performer."""
    first = re.split(
        r"\s*[,;&×+]\s*|\s+(?:x|feat\.?|ft\.?|featuring|with)\s+",
        artist, maxsplit=1, flags=re.IGNORECASE,
    )[0]
    first = re.sub(r"^official\s+|\s*vevo$|\s*-\s*topic$", "", first, flags=re.IGNORECASE)
    first = first.strip() or artist
    if first.casefold() in {"gruppa skryptonite", "группа скриптонит"}:
        return "Скриптонит"
    return first


def credited_artists(artist: str, title: str) -> list[str]:
    """Read artist credits from metadata and feat credits from the track title."""
    parts = re.split(r"\s*[,;&×+]\s*|\s+(?:x|feat\.?|ft\.?|featuring|with)\s+", artist, flags=re.IGNORECASE)
    for match in re.finditer(r"\b(?:feat\.?|ft\.?|featuring)\s+([^\])]+)", title, flags=re.IGNORECASE):
        parts.extend(re.split(r"\s*[,;&×+]\s*|\s+\bx\b\s+", match.group(1), flags=re.IGNORECASE))
    credits = []
    for part in parts:
        name = primary_artist(part.strip(" .()[]"))
        if name and name.casefold() not in {credit.casefold() for credit in credits}:
            credits.append(name)
    return credits or [primary_artist(artist)]


def catalog_artist(artist: str, title: str, counts: dict[str, int]) -> str:
    credits = credited_artists(artist, title)
    return max(credits, key=lambda name: counts.get(name.casefold(), 0))


def artist_appearance_counts(rows: list[tuple[str, str, str]]) -> dict[str, int]:
    counts = {}
    seen = set()
    for identity, artist, title in rows:
        if identity in seen:
            continue
        seen.add(identity)
        for name in credited_artists(artist, title):
            key = name.casefold()
            counts[key] = counts.get(key, 0) + 1
    return counts


def assigned_artist(chat_id: int, artist: str, title: str, db_path: Path = HISTORY_DB) -> str:
    with closing(history_connection(db_path)) as connection:
        rows = connection.execute(
            "SELECT video_id, artist, title "
            "FROM sent_tracks WHERE chat_id = ? AND artist IS NOT NULL AND title IS NOT NULL",
            (chat_id,),
        ).fetchall()
    return catalog_artist(artist, title, artist_appearance_counts([
        (song_key(saved_artist, saved_title) or video_id, saved_artist, saved_title)
        for video_id, saved_artist, saved_title in rows
    ]))


def history_connection(db_path: Path = HISTORY_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS sent_tracks ("
        "chat_id INTEGER NOT NULL, video_id TEXT NOT NULL, song_key TEXT NOT NULL, "
        "sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (chat_id, video_id))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS artist_topics ("
        "chat_id INTEGER NOT NULL, artist_key TEXT NOT NULL, "
        "message_thread_id INTEGER NOT NULL, "
        "PRIMARY KEY (chat_id, artist_key))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS retry_batches ("
        "job_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, "
        "source_message_id INTEGER NOT NULL, media_type TEXT NOT NULL, "
        "urls_json TEXT NOT NULL, known_keys_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS shared_playlists ("
        "owner_chat_id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS shared_recipient_subscriptions ("
        "owner_chat_id INTEGER NOT NULL, recipient_chat_id INTEGER NOT NULL, "
        "token TEXT NOT NULL, status_message_id INTEGER, pending INTEGER NOT NULL DEFAULT 1, "
        "last_error TEXT, PRIMARY KEY (owner_chat_id, recipient_chat_id))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS shared_recipient_topics ("
        "owner_chat_id INTEGER NOT NULL, recipient_chat_id INTEGER NOT NULL, "
        "topic_key TEXT NOT NULL, message_thread_id INTEGER NOT NULL, "
        "PRIMARY KEY (owner_chat_id, recipient_chat_id, topic_key))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS shared_recipient_tracks ("
        "owner_chat_id INTEGER NOT NULL, recipient_chat_id INTEGER NOT NULL, "
        "track_key TEXT NOT NULL, video_id TEXT NOT NULL, topic_key TEXT NOT NULL, "
        "message_id INTEGER NOT NULL, "
        "PRIMARY KEY (owner_chat_id, recipient_chat_id, track_key))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS transient_message_cleanup ("
        "chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, due_at REAL NOT NULL, "
        "active INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (chat_id, message_id))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS favorite_copies ("
        "chat_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL, "
        "favorite_message_id INTEGER NOT NULL, pending_delete INTEGER NOT NULL DEFAULT 0, "
        "artist TEXT, title TEXT, "
        "PRIMARY KEY (chat_id, source_message_id))"
    )
    favorite_columns = {row[1] for row in connection.execute("PRAGMA table_info(favorite_copies)")}
    for name in ("artist", "title"):
        if name not in favorite_columns:
            connection.execute(f"ALTER TABLE favorite_copies ADD COLUMN {name} TEXT")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(sent_tracks)")}
    for name in ("artist", "title", "audio_file_id", "video_file_id"):
        if name not in columns:
            connection.execute(f"ALTER TABLE sent_tracks ADD COLUMN {name} TEXT")
    for name in ("message_id", "message_thread_id"):
        if name not in columns:
            connection.execute(f"ALTER TABLE sent_tracks ADD COLUMN {name} INTEGER")
    if "favorite" not in columns:
        connection.execute("ALTER TABLE sent_tracks ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
    retry_columns = {row[1] for row in connection.execute("PRAGMA table_info(retry_batches)")}
    if "quality" not in retry_columns:
        connection.execute("ALTER TABLE retry_batches ADD COLUMN quality TEXT")
    db_path.chmod(0o600)
    return connection


def save_retry_batch(
    job_id: str, user_id: int, chat_id: int, source_message_id: int,
    media_type: str, urls: list[str], known_keys: dict[str, str], db_path: Path = HISTORY_DB,
    *, quality: str | None = None,
) -> None:
    with closing(history_connection(db_path)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO retry_batches (job_id, user_id, chat_id, source_message_id, media_type, urls_json, known_keys_json, quality) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
                "urls_json = excluded.urls_json, known_keys_json = excluded.known_keys_json, quality = excluded.quality",
                (job_id, user_id, chat_id, source_message_id, media_type,
                 json.dumps(urls), json.dumps(known_keys), quality or DEFAULT_QUALITY[media_type]),
            )


def load_retry_batch(job_id: str, db_path: Path = HISTORY_DB) -> dict | None:
    with closing(history_connection(db_path)) as connection:
        row = connection.execute(
            "SELECT user_id, chat_id, source_message_id, media_type, urls_json, known_keys_json, quality "
            "FROM retry_batches WHERE job_id = ?", (job_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "user_id": row[0], "chat_id": row[1], "source_message_id": row[2],
        "media_type": row[3], "urls": json.loads(row[4]), "known_keys": json.loads(row[5]),
        "quality": row[6] or DEFAULT_QUALITY[row[3]],
    }


def delete_retry_batch(job_id: str, db_path: Path = HISTORY_DB) -> None:
    with closing(history_connection(db_path)) as connection:
        with connection:
            connection.execute("DELETE FROM retry_batches WHERE job_id = ?", (job_id,))


def sent_tracks(chat_id: int, db_path: Path = HISTORY_DB) -> tuple[set[str], set[str]]:
    with closing(history_connection(db_path)) as connection:
        rows = connection.execute(
            "SELECT video_id, song_key, artist, title FROM sent_tracks WHERE chat_id = ?", (chat_id,)
        ).fetchall()
    keys = {song_key(row[2], row[3]) for row in rows if row[2] and row[3]}
    keys.update(row[1] for row in rows if row[1])
    return {row[0] for row in rows}, {key for key in keys if key}


def filter_sent_tracks(
    chat_id: int, entries: list[tuple[str, str, str]], db_path: Path = HISTORY_DB
) -> tuple[list[tuple[str, str, str]], int]:
    available, reasons = filter_sent_tracks_explained(chat_id, entries, db_path)
    return available, len(reasons)


def filter_sent_tracks_explained(
    chat_id: int, entries: list[tuple[str, str, str]], db_path: Path = HISTORY_DB,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Return available tracks and the specific catalog/list item behind each skip."""
    with closing(history_connection(db_path)) as connection:
        rows = connection.execute(
            "SELECT video_id, song_key, artist, title FROM sent_tracks WHERE chat_id = ?", (chat_id,)
        ).fetchall()
    by_video = {}
    by_song = {}
    for video_id, saved_key, artist, title in rows:
        label = f"{artist or 'Без исполнителя'} — {title or video_id}"
        match = (label, "в чате")
        by_video[video_id] = match
        for key in (saved_key, song_key(artist, title) if artist and title else ""):
            if key:
                by_song.setdefault(key, match)
    available = []
    reasons = []
    for url, label, key in entries:
        video_id = parse_qs(urlsplit(url).query)["v"][0]
        match = by_video.get(video_id) or (by_song.get(key) if key else None)
        if match:
            reasons.append((label, *match))
            continue
        available.append((url, label, key))
        match = (label, "в этом списке")
        by_video[video_id] = match
        if key:
            by_song.setdefault(key, match)
    return available, reasons


def already_sent(chat_id: int, url: str, key: str = "") -> bool:
    video_ids, song_keys = sent_tracks(chat_id)
    video_id = parse_qs(urlsplit(url).query)["v"][0]
    return video_id in video_ids or bool(key and key in song_keys)


def record_sent_key(
    chat_id: int, url: str, key: str, db_path: Path = HISTORY_DB,
    *, artist: str | None = None, title: str | None = None,
    audio_file_id: str | None = None, video_file_id: str | None = None,
    message_id: int | None = None, message_thread_id: int | None = None,
) -> None:
    video_id = parse_qs(urlsplit(url).query)["v"][0]
    with closing(history_connection(db_path)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO sent_tracks (chat_id, video_id, song_key, artist, title, audio_file_id, video_file_id, message_id, message_thread_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id, video_id) DO UPDATE SET "
                "song_key = excluded.song_key, "
                "artist = COALESCE(excluded.artist, sent_tracks.artist), "
                "title = COALESCE(excluded.title, sent_tracks.title), "
                "audio_file_id = COALESCE(excluded.audio_file_id, sent_tracks.audio_file_id), "
                "video_file_id = COALESCE(excluded.video_file_id, sent_tracks.video_file_id), "
                "message_id = COALESCE(excluded.message_id, sent_tracks.message_id), "
                "message_thread_id = COALESCE(excluded.message_thread_id, sent_tracks.message_thread_id)",
                (chat_id, video_id, key, artist, title, audio_file_id, video_file_id, message_id, message_thread_id),
            )


def record_sent_track(
    chat_id: int, url: str, artist: str | None, title: str, db_path: Path = HISTORY_DB,
    *, file_id: str | None = None, media_type: str = "mp3",
    message_id: int | None = None, message_thread_id: int | None = None,
) -> None:
    record_sent_key(
        chat_id, url, song_key(artist, title), db_path,
        artist=artist, title=title,
        audio_file_id=file_id if media_type == "mp3" else None,
        video_file_id=file_id if media_type == "mp4" else None,
        message_id=message_id, message_thread_id=message_thread_id,
    )


def set_favorite(chat_id: int, message_id: int, enabled: bool, db_path: Path = HISTORY_DB) -> bool:
    with closing(history_connection(db_path)) as connection:
        with connection:
            changed = connection.execute(
                "UPDATE sent_tracks SET favorite = ? WHERE chat_id = ? AND message_id = ? ",
                (int(enabled), chat_id, message_id),
            ).rowcount
    return bool(changed)


def favorite_lock(app: Application, chat_id: int) -> asyncio.Lock:
    locks = app.bot_data.setdefault("favorite_locks", {})
    return locks.setdefault(chat_id, asyncio.Lock())


def favorite_topic_id(chat_id: int) -> int | None:
    with closing(history_connection()) as connection:
        row = connection.execute(
            "SELECT message_thread_id FROM artist_topics WHERE chat_id=? AND artist_key=?",
            (chat_id, FAVORITES_TOPIC_KEY),
        ).fetchone()
    return row[0] if row else None


async def ensure_favorite_topic(app: Application, chat_id: int) -> int:
    thread_id = favorite_topic_id(chat_id)
    if thread_id is not None:
        return thread_id
    topic = await app.bot.create_forum_topic(chat_id=chat_id, name=FAVORITES_TOPIC_NAME)
    thread_id = topic.message_thread_id
    with closing(history_connection()) as connection:
        with connection:
            connection.execute(
                "INSERT INTO artist_topics (chat_id, artist_key, message_thread_id) VALUES (?, ?, ?)",
                (chat_id, FAVORITES_TOPIC_KEY, thread_id),
            )
    return thread_id


async def delete_favorite_copy(app: Application, chat_id: int, source_id: int, copy_id: int) -> None:
    for attempt in range(3):
        try:
            client = app.bot_data.get("reaction_client")
            if client is not None:
                try:
                    await client.delete_messages(chat_id, [copy_id], revoke=True)
                except (RPCError, OSError, ValueError) as exc:
                    LOG.warning("MTProto removal of favorite copy %s failed: %s", copy_id, exc)
                    await app.bot.delete_message(chat_id=chat_id, message_id=copy_id)
            else:
                await app.bot.delete_message(chat_id=chat_id, message_id=copy_id)
        except RetryAfter as exc:
            wait = exc.retry_after
            await asyncio.sleep(wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait))
        except NetworkError:
            await asyncio.sleep(2 ** attempt)
        except BadRequest as exc:
            if "not found" not in str(exc).casefold():
                LOG.warning("Could not remove favorite copy %s: %s", copy_id, exc)
                return
            break
        except TelegramError as exc:
            LOG.warning("Could not remove favorite copy %s: %s", copy_id, exc)
            return
        else:
            break
    else:
        LOG.warning("Favorite copy %s will be removed after retry", copy_id)
        return
    with closing(history_connection()) as connection:
        with connection:
            connection.execute(
                "DELETE FROM favorite_copies WHERE chat_id=? AND source_message_id=? "
                "AND favorite_message_id=? AND pending_delete=1",
                (chat_id, source_id, copy_id),
            )


async def retry_pending_favorite_deletions(app: Application) -> None:
    with closing(history_connection()) as connection:
        rows = connection.execute(
            "SELECT chat_id, source_message_id, favorite_message_id FROM favorite_copies "
            "WHERE pending_delete=1"
        ).fetchall()
    for chat_id, source_id, copy_id in rows:
        async with favorite_lock(app, chat_id):
            with closing(history_connection()) as connection:
                pending = connection.execute(
                    "SELECT pending_delete FROM favorite_copies WHERE chat_id=? AND source_message_id=? "
                    "AND favorite_message_id=?",
                    (chat_id, source_id, copy_id),
                ).fetchone()
            if pending and pending[0]:
                await delete_favorite_copy(app, chat_id, source_id, copy_id)


async def favorite_source_details(app: Application, chat_id: int, source_id: int, row):
    if row and row[0]:
        return row[1], row[2], row[3]
    client = app.bot_data.get("reaction_client")
    if client is None:
        return None
    try:
        message = await client.get_messages(chat_id, ids=source_id)
    except (RPCError, OSError, ValueError) as exc:
        LOG.warning("Could not inspect reacted message %s: %s", source_id, exc)
        return None
    if message is None or not message.audio:
        return None
    thread_id = getattr(getattr(message, "reply_to", None), "reply_to_top_id", None)
    return (
        thread_id,
        getattr(message.file, "performer", None) or "Без исполнителя",
        getattr(message.file, "title", None) or getattr(message.file, "name", None) or "Без названия",
    )


async def sync_favorite_copy(app: Application, chat_id: int, source_id: int, enabled: bool) -> bool:
    """Keep a bot audio message and its favorite-topic copy in sync."""
    async with favorite_lock(app, chat_id):
        with closing(history_connection()) as connection:
            source = connection.execute(
                "SELECT audio_file_id, message_thread_id, artist, title FROM sent_tracks "
                "WHERE chat_id=? AND message_id=?",
                (chat_id, source_id),
            ).fetchone()
            copy = connection.execute(
                "SELECT favorite_message_id, pending_delete FROM favorite_copies "
                "WHERE chat_id=? AND source_message_id=?",
                (chat_id, source_id),
            ).fetchone()
            is_favorite_copy = connection.execute(
                "SELECT 1 FROM favorite_copies WHERE chat_id=? AND favorite_message_id=? LIMIT 1",
                (chat_id, source_id),
            ).fetchone()
        if is_favorite_copy:
            return False
        if enabled:
            if copy and copy[0] > 0:
                with closing(history_connection()) as connection:
                    with connection:
                        connection.execute(
                            "UPDATE sent_tracks SET favorite=1 WHERE chat_id=? AND message_id=?",
                            (chat_id, source_id),
                        )
                        connection.execute(
                            "UPDATE favorite_copies SET pending_delete=0 "
                            "WHERE chat_id=? AND source_message_id=?",
                            (chat_id, source_id),
                        )
                return True
            details = await favorite_source_details(app, chat_id, source_id, source)
            if details is None:
                return False
            source_thread, artist, title = details
            thread_id = favorite_topic_id(chat_id)
            if thread_id is not None and source_thread == thread_id:
                return False
            with closing(history_connection()) as connection:
                with connection:
                    connection.execute(
                        "UPDATE sent_tracks SET favorite=1 WHERE chat_id=? AND message_id=?",
                        (chat_id, source_id),
                    )
                    connection.execute(
                        "INSERT INTO favorite_copies "
                        "(chat_id, source_message_id, favorite_message_id, pending_delete, artist, title) "
                        "VALUES (?, ?, 0, 0, ?, ?) ON CONFLICT(chat_id, source_message_id) "
                        "DO UPDATE SET pending_delete=0, artist=excluded.artist, title=excluded.title",
                        (chat_id, source_id, artist, title),
                    )
            thread_id = await ensure_favorite_topic(app, chat_id)
            try:
                copied = await app.bot.copy_message(
                    chat_id=chat_id, from_chat_id=chat_id, message_id=source_id,
                    message_thread_id=thread_id, disable_notification=True,
                )
            except TelegramError as exc:
                LOG.warning("Could not copy favorite %s in chat %s: %s", source_id, chat_id, exc)
                return True
            with closing(history_connection()) as connection:
                with connection:
                    connection.execute(
                        "UPDATE favorite_copies SET favorite_message_id=? "
                        "WHERE chat_id=? AND source_message_id=? AND pending_delete=0",
                        (copied.message_id, chat_id, source_id),
                    )
            return True
        if not source and not copy:
            return False
        with closing(history_connection()) as connection:
            with connection:
                connection.execute(
                    "UPDATE sent_tracks SET favorite=0 WHERE chat_id=? AND message_id=?",
                    (chat_id, source_id),
                )
                connection.execute(
                    "UPDATE favorite_copies SET pending_delete=1 "
                    "WHERE chat_id=? AND source_message_id=?",
                    (chat_id, source_id),
                )
                if copy and copy[0] == 0:
                    connection.execute(
                        "DELETE FROM favorite_copies WHERE chat_id=? AND source_message_id=?",
                        (chat_id, source_id),
                    )
        if copy and copy[0] > 0:
            await delete_favorite_copy(app, chat_id, source_id, copy[0])
        return True


async def backfill_favorite_topic(app: Application) -> None:
    await retry_pending_favorite_deletions(app)
    with closing(history_connection()) as connection:
        rows = set(connection.execute(
            "SELECT chat_id, message_id FROM sent_tracks "
            "WHERE favorite=1 AND audio_file_id IS NOT NULL AND message_id IS NOT NULL"
        ).fetchall())
        rows.update(connection.execute(
            "SELECT chat_id, source_message_id FROM favorite_copies "
            "WHERE favorite_message_id=0 AND pending_delete=0"
        ).fetchall())
    for chat_id, source_id in rows:
        try:
            await sync_favorite_copy(app, chat_id, source_id, True)
        except (sqlite3.Error, OSError, TelegramError) as exc:
            LOG.warning("Could not restore favorite %s in chat %s: %s", source_id, chat_id, exc)
        await asyncio.sleep(0.3)


async def reconcile_favorite_topic(app: Application) -> None:
    while True:
        try:
            await backfill_favorite_topic(app)
        except (sqlite3.Error, OSError, TelegramError) as exc:
            LOG.warning("Could not reconcile favorite topic: %s", exc)
        await asyncio.sleep(60)


def favorite_tracks(chat_id: int, db_path: Path = HISTORY_DB) -> list[tuple[str, str]]:
    with closing(history_connection(db_path)) as connection:
        rows = connection.execute(
            "SELECT artist, title, song_key, video_id FROM sent_tracks "
            "WHERE chat_id = ? AND favorite = 1 ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE",
            (chat_id,),
        ).fetchall()
        untracked = connection.execute(
            "SELECT artist, title FROM favorite_copies AS copy "
            "WHERE copy.chat_id=? AND copy.pending_delete=0 "
            "AND NOT EXISTS (SELECT 1 FROM sent_tracks AS track "
            "WHERE track.chat_id=copy.chat_id AND track.message_id=copy.source_message_id) "
            "ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE",
            (chat_id,),
        ).fetchall()
    return [
        (artist or key.partition("|")[0] or "Без исполнителя", title or key.partition("|")[2] or video_id)
        for artist, title, key, video_id in rows
    ] + [(artist or "Без исполнителя", title or "Без названия") for artist, title in untracked]


async def artist_topic(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, artist: str | None, *, misc: bool = False,
) -> int:
    """Create one Telegram topic per lead artist in a private chat."""
    name = MISC_TOPIC_NAME if misc else primary_artist(artist or "Без исполнителя")
    key = "__misc__" if misc else unicodedata.normalize("NFKC", name).casefold()
    lock = context.application.bot_data.setdefault("artist_topic_lock", asyncio.Lock())
    async with lock:
        with closing(history_connection()) as connection:
            row = connection.execute(
                "SELECT message_thread_id FROM artist_topics WHERE chat_id = ? AND artist_key = ?",
                (chat_id, key),
            ).fetchone()
        if row:
            return row[0]
        if key == "скриптонит":
            with closing(history_connection()) as connection:
                old = connection.execute(
                    "SELECT message_thread_id FROM artist_topics WHERE chat_id = ? "
                    "AND artist_key IN ('gruppa skryptonite', 'группа скриптонит') LIMIT 1",
                    (chat_id,),
                ).fetchone()
                if old:
                    with connection:
                        connection.execute(
                            "INSERT INTO artist_topics (chat_id, artist_key, message_thread_id) VALUES (?, ?, ?)",
                            (chat_id, key, old[0]),
                        )
                    return old[0]
        topic = await context.bot.create_forum_topic(chat_id=chat_id, name=name)
        with closing(history_connection()) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO artist_topics (chat_id, artist_key, message_thread_id) VALUES (?, ?, ?)",
                    (chat_id, key, topic.message_thread_id),
                )
        return topic.message_thread_id


def artist_track_count(chat_id: int, artist: str) -> int:
    key = unicodedata.normalize("NFKC", primary_artist(artist)).casefold()
    for group_artist, tracks in library_groups(chat_id):
        if unicodedata.normalize("NFKC", group_artist).casefold() == key:
            return len(tracks)
    return 0


async def move_recent_misc_tracks(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, artist: str, misc_thread: int, artist_thread: int,
) -> None:
    """Move recent tracks into a new artist topic without leaving duplicates."""
    with closing(history_connection()) as connection:
        rows = connection.execute(
            "SELECT video_id, artist, song_key, message_id FROM sent_tracks "
            "WHERE chat_id = ? AND message_thread_id = ? AND message_id IS NOT NULL "
            "AND sent_at >= datetime('now', '-47 hours') ORDER BY sent_at",
            (chat_id, misc_thread),
        ).fetchall()
    target = unicodedata.normalize("NFKC", primary_artist(artist)).casefold()
    for video_id, saved_artist, key, old_message_id in rows:
        source_artist = saved_artist or key.partition("|")[0]
        with closing(history_connection()) as connection:
            title_row = connection.execute(
                "SELECT title FROM sent_tracks WHERE chat_id = ? AND video_id = ?", (chat_id, video_id),
            ).fetchone()
        source_title = (title_row[0] if title_row else None) or ""
        if unicodedata.normalize("NFKC", assigned_artist(chat_id, source_artist, source_title)).casefold() != target:
            continue
        try:
            copied = await context.bot.copy_message(
                chat_id=chat_id, from_chat_id=chat_id, message_id=old_message_id,
                message_thread_id=artist_thread, disable_notification=True,
            )
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=old_message_id)
            except TelegramError:
                await context.bot.delete_message(chat_id=chat_id, message_id=copied.message_id)
                raise
            with closing(history_connection()) as connection:
                with connection:
                    connection.execute(
                        "UPDATE sent_tracks SET message_id = ?, message_thread_id = ?, sent_at = CURRENT_TIMESTAMP "
                        "WHERE chat_id = ? AND video_id = ?",
                        (copied.message_id, artist_thread, chat_id, video_id),
                    )
                    connection.execute(
                        "UPDATE favorite_copies SET source_message_id=? "
                        "WHERE chat_id=? AND source_message_id=?",
                        (copied.message_id, chat_id, old_message_id),
                    )
        except (TelegramError, sqlite3.Error, OSError) as exc:
            LOG.warning("Could not move track %s to artist topic: %s", video_id, exc)


def library_groups(chat_id: int, db_path: Path = HISTORY_DB) -> list[tuple[str, list[dict]]]:
    """Return saved songs grouped by artist, with repeated songs shown once."""
    with closing(history_connection(db_path)) as connection:
        rows = connection.execute(
            "SELECT video_id, song_key, artist, title, audio_file_id, video_file_id "
            "FROM sent_tracks WHERE chat_id = ? "
            "ORDER BY (audio_file_id IS NOT NULL) DESC, sent_at DESC", (chat_id,)
        ).fetchall()
    counts = artist_appearance_counts([
        (song_key(artist, title) if artist and title else key or video_id,
         artist or key.partition("|")[0] or "Без исполнителя", title or key.partition("|")[2] or video_id)
        for video_id, key, artist, title, _, _ in rows
    ])
    groups = {}
    seen_songs = set()
    for video_id, key, artist, title, audio_file_id, video_file_id in rows:
        fallback_artist, _, fallback_title = key.partition("|")
        artist = artist or fallback_artist or "Без исполнителя"
        title = title or fallback_title or video_id
        identity = song_key(artist, title) or key or video_id
        if identity in seen_songs:
            continue
        seen_songs.add(identity)
        group_artist = catalog_artist(artist, title, counts)
        group_key = unicodedata.normalize("NFKC", group_artist).casefold()
        group = groups.setdefault(group_key, [group_artist, []])
        group[1].append({
            "video_id": video_id,
            "song_key": key,
            "artist": artist,
            "title": title,
            "audio_file_id": audio_file_id,
            "video_file_id": video_file_id,
        })
    result = [(artist, sorted(tracks, key=lambda track: track["title"].casefold())) for artist, tracks in groups.values()]
    return sorted(result, key=lambda group: group[0].casefold())


def topic_library_groups(chat_id: int, db_path: Path = HISTORY_DB) -> list[tuple[str, list[dict]]]:
    """Show the same five-song threshold as Telegram's topic list."""
    dedicated = []
    misc = []
    for artist, tracks in library_groups(chat_id, db_path):
        if len(tracks) >= ARTIST_TOPIC_THRESHOLD:
            dedicated.append((artist, tracks))
        else:
            for track in tracks:
                misc.append({**track, "title": f"{artist} — {track['title']}"})
    if misc:
        dedicated.append((MISC_TOPIC_NAME, sorted(misc, key=lambda track: track["title"].casefold())))
    return sorted(dedicated, key=lambda group: group[0].casefold())


def get_or_create_share_token(chat_id: int, db_path: Path = HISTORY_DB) -> str:
    with closing(history_connection(db_path)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO shared_playlists (owner_chat_id, token) VALUES (?, ?) "
                "ON CONFLICT(owner_chat_id) DO NOTHING",
                (chat_id, secrets.token_urlsafe(24)),
            )
            return connection.execute(
                "SELECT token FROM shared_playlists WHERE owner_chat_id = ?", (chat_id,)
            ).fetchone()[0]


def revoke_share_token(chat_id: int, db_path: Path = HISTORY_DB) -> bool:
    with closing(history_connection(db_path)) as connection:
        with connection:
            revoked = bool(connection.execute(
                "DELETE FROM shared_playlists WHERE owner_chat_id = ?", (chat_id,)
            ).rowcount)
            connection.execute(
                "UPDATE shared_recipient_subscriptions SET pending = 0, last_error = 'revoked' "
                "WHERE owner_chat_id = ?", (chat_id,)
            )
            return revoked


def share_owner(token: str, db_path: Path = HISTORY_DB) -> int | None:
    if not SHARE_TOKEN.fullmatch(token):
        return None
    with closing(history_connection(db_path)) as connection:
        row = connection.execute(
            "SELECT owner_chat_id FROM shared_playlists WHERE token = ?", (token,)
        ).fetchone()
    return row[0] if row else None


def shared_groups(chat_id: int, db_path: Path = HISTORY_DB) -> list[tuple[str, list[dict]]]:
    return [
        (artist, available)
        for artist, tracks in library_groups(chat_id, db_path)
        if (available := [track for track in tracks if track["audio_file_id"]])
    ]


def zip_markup(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📦 Скачать всё ZIP", callback_data=f"zip:{token}")
    ]])


def shared_topic_groups(chat_id: int, db_path: Path = HISTORY_DB) -> list[tuple[str, str, list[dict]]]:
    """Match artist-topic thresholds while keeping original MP3 performer and title."""
    groups = []
    misc = []
    for artist, tracks in library_groups(chat_id, db_path):
        audio_tracks = [track for track in tracks if track["audio_file_id"]]
        if len(tracks) >= ARTIST_TOPIC_THRESHOLD:
            if audio_tracks:
                key = "artist:" + unicodedata.normalize("NFKC", artist).casefold()
                groups.append((key, artist, audio_tracks))
        else:
            misc.extend(audio_tracks)
    if misc:
        groups.append(("__misc__", MISC_TOPIC_NAME, sorted(misc, key=lambda track: track["title"].casefold())))
    return sorted(groups, key=lambda group: group[1].casefold())


def save_shared_subscription(
    owner: int, recipient: int, token: str, status_message_id: int | None,
    db_path: Path = HISTORY_DB,
) -> None:
    with closing(history_connection(db_path)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO shared_recipient_subscriptions "
                "(owner_chat_id, recipient_chat_id, token, status_message_id, pending, last_error) "
                "VALUES (?, ?, ?, ?, 1, NULL) "
                "ON CONFLICT(owner_chat_id, recipient_chat_id) DO UPDATE SET "
                "token = excluded.token, status_message_id = excluded.status_message_id, "
                "pending = 1, last_error = NULL",
                (owner, recipient, token, status_message_id),
            )


def set_shared_subscription_state(
    owner: int, recipient: int, token: str, pending: bool, error: str | None,
    db_path: Path = HISTORY_DB,
) -> None:
    with closing(history_connection(db_path)) as connection:
        with connection:
            connection.execute(
                "UPDATE shared_recipient_subscriptions SET pending = ?, last_error = ? "
                "WHERE owner_chat_id = ? AND recipient_chat_id = ? AND token = ?",
                (int(pending), error, owner, recipient, token),
            )


def pending_shared_subscriptions(db_path: Path = HISTORY_DB) -> list[tuple[int, int, str, int | None]]:
    with closing(history_connection(db_path)) as connection:
        return connection.execute(
            "SELECT s.owner_chat_id, s.recipient_chat_id, s.token, s.status_message_id "
            "FROM shared_recipient_subscriptions AS s "
            "JOIN shared_playlists AS p ON p.owner_chat_id = s.owner_chat_id AND p.token = s.token "
            "WHERE s.pending = 1"
        ).fetchall()


def shared_recipient_topic(owner: int, recipient: int, key: str, db_path: Path = HISTORY_DB) -> int | None:
    with closing(history_connection(db_path)) as connection:
        row = connection.execute(
            "SELECT message_thread_id FROM shared_recipient_topics "
            "WHERE owner_chat_id = ? AND recipient_chat_id = ? AND topic_key = ?",
            (owner, recipient, key),
        ).fetchone()
    return row[0] if row else None


def record_shared_recipient_topic(
    owner: int, recipient: int, key: str, thread: int, db_path: Path = HISTORY_DB,
) -> None:
    with closing(history_connection(db_path)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO shared_recipient_topics "
                "(owner_chat_id, recipient_chat_id, topic_key, message_thread_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(owner_chat_id, recipient_chat_id, topic_key) DO UPDATE SET "
                "message_thread_id = excluded.message_thread_id",
                (owner, recipient, key, thread),
            )


def forget_shared_recipient_topic(
    owner: int, recipient: int, key: str, db_path: Path = HISTORY_DB,
) -> None:
    """A deleted Telegram topic invalidates all copies recorded inside it."""
    with closing(history_connection(db_path)) as connection:
        with connection:
            connection.execute(
                "DELETE FROM shared_recipient_topics WHERE owner_chat_id = ? AND recipient_chat_id = ? AND topic_key = ?",
                (owner, recipient, key),
            )
            connection.execute(
                "DELETE FROM shared_recipient_tracks WHERE owner_chat_id = ? AND recipient_chat_id = ? AND topic_key = ?",
                (owner, recipient, key),
            )


def shared_recipient_track(
    owner: int, recipient: int, track_key: str, db_path: Path = HISTORY_DB,
) -> tuple[str, int] | None:
    with closing(history_connection(db_path)) as connection:
        return connection.execute(
            "SELECT topic_key, message_id FROM shared_recipient_tracks "
            "WHERE owner_chat_id = ? AND recipient_chat_id = ? AND track_key = ?",
            (owner, recipient, track_key),
        ).fetchone()


def record_shared_recipient_track(
    owner: int, recipient: int, track_key: str, video_id: str, topic_key: str, message_id: int,
    db_path: Path = HISTORY_DB,
) -> None:
    with closing(history_connection(db_path)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO shared_recipient_tracks "
                "(owner_chat_id, recipient_chat_id, track_key, video_id, topic_key, message_id) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_chat_id, recipient_chat_id, track_key) DO UPDATE SET "
                "video_id = excluded.video_id, topic_key = excluded.topic_key, "
                "message_id = excluded.message_id",
                (owner, recipient, track_key, video_id, topic_key, message_id),
            )


def tag_mp3(path: Path, artist: str | None, title: str, cover: Path | None = None) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.setall("TIT2", [TIT2(encoding=3, text=title)])
    if artist:
        tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
    if cover:
        tags.setall("APIC", [APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover.read_bytes())])
    tags.save(path)


def youtube_cover(url: str, directory: Path) -> Path | None:
    """Fetch a small YouTube preview; cover failures must not block the audio."""
    video_id = parse_qs(urlsplit(url).query)["v"][0]
    raw = directory / "thumbnail-original.jpg"
    cover = directory / "cover.jpg"
    try:
        request = Request(f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=5) as response:
            data = response.read(2_000_001)
        if not data or len(data) > 2_000_000:
            return None
        raw.write_bytes(data)
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw),
             "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
             "-frames:v", "1", "-q:v", "8", str(cover)],
            check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return cover if cover.stat().st_size < 200_000 else None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        LOG.info("Could not prepare YouTube cover: %s", exc)
        return None


def display_filename(artist: str | None, title: str, media_type: str) -> str:
    label = f"{artist} - {title}" if artist else title
    label = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", label).strip(" .")[:150]
    return f"{label or 'YouTube'}.{media_type}"


def download_media(
    url: str, media_type: str, directory: Path, quality: str | None = None,
) -> tuple[Path, str | None, str, Path | None]:
    """Run blocking yt-dlp work in a worker thread; return the final media file."""
    quality = quality or DEFAULT_QUALITY[media_type]
    if quality not in QUALITY_OPTIONS[media_type]:
        raise ValueError("Неподдерживаемое качество файла.")
    options = {
        "outtmpl": str(directory / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "no_warnings": True,
        "noprogress": True,
        "quiet": True,
        "max_filesize": MAX_FILE_BYTES,
    }
    if media_type == "mp3":
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality}],
        })
    else:
        options.update({
            "format": f"bv[height<={quality}][vcodec^=avc1]+ba[ext=m4a]/b[height<={quality}][ext=mp4]",
            "merge_output_format": "mp4",
        })

    with YoutubeDL(options) as downloader:
        preview = downloader.extract_info(url, download=False)
        if not preview or preview.get("is_live") or preview.get("live_status") in {"is_live", "is_upcoming"}:
            raise ValueError("Прямые трансляции не поддерживаются.")
        info = downloader.extract_info(url, download=True)

    if not info:
        raise ValueError("Не удалось получить видео.")
    files = list(directory.glob(f"*.{media_type}"))
    if not files:
        raise ValueError("Не удалось создать файл нужного формата. Проверьте ffmpeg и обновите yt-dlp.")
    file_path = files[0]
    artist, title = music_metadata(info)
    cover = None
    if media_type == "mp3":
        cover = youtube_cover(url, directory)
        tag_mp3(file_path, artist, title, cover)
    if file_path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Файл больше 50 МБ. Попробуйте более короткое видео.")
    return file_path, artist, title, cover


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args and context.args[0].startswith("share_"):
        await open_shared_playlist(update, context, context.args[0][6:])
        return
    await update.effective_message.reply_text(
        "Пришлите одну или несколько ссылок на видео YouTube (до 10 в одном сообщении). "
        "Если пришлёте ссылку на Mix, можно будет выбрать треки из списка или скачать 30 новых MP3 одной кнопкой. "
        "Я предложу скачать MP3 или MP4 и выбрать качество. "
        "Файл должен быть не больше 50 МБ. Во время загрузки можно нажать «Отменить», "
        "а неудачные ссылки повторить кнопкой. /artists — разделы музыки, /search — поиск, "
        "/stats — статистика, /findout запрос — поиск на YouTube. "
        "Ответьте /favorite на песню, чтобы добавить её в /favorites. "
        "/zip — скачать свою библиотеку ZIP частями, /share — ссылка на подборку MP3. "
        "В нужной теме отправьте /renametopic Новое название, чтобы переименовать её."
    )


def queue_transient_cleanup(chat_id: int, message_ids: tuple[int, ...], delay: float, *, active=False) -> None:
    with closing(history_connection()) as connection:
        with connection:
            connection.executemany(
                "INSERT INTO transient_message_cleanup (chat_id, message_id, due_at, active) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(chat_id, message_id) DO UPDATE SET "
                "due_at=excluded.due_at, active=excluded.active",
                ((chat_id, message_id, time.time() + delay, int(active)) for message_id in message_ids),
            )


def schedule_transient_cleanup(bot, chat_id: int, message_ids: tuple[int, ...], delay: float) -> None:
    queue_transient_cleanup(chat_id, message_ids, delay)
    asyncio.create_task(delete_transient_messages(bot, chat_id, message_ids, delay))


async def delete_transient_messages(bot, chat_id: int, message_ids: tuple[int, ...], delay: float) -> None:
    await asyncio.sleep(delay)
    for message_id in message_ids:
        while True:
            for attempt in range(3):
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=message_id)
                except RetryAfter as exc:
                    wait = exc.retry_after
                    wait = wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
                    wait = max(0, wait)
                    with closing(history_connection()) as connection:
                        with connection:
                            connection.execute(
                                "UPDATE transient_message_cleanup SET due_at=? WHERE chat_id=? AND message_id=?",
                                (time.time() + wait, chat_id, message_id),
                            )
                    await asyncio.sleep(wait)
                except NetworkError:
                    await asyncio.sleep(2 ** attempt)
                except TelegramError as exc:
                    LOG.debug("Could not delete transient message %s in chat %s: %s", message_id, chat_id, exc)
                    break
                else:
                    break
            else:
                with closing(history_connection()) as connection:
                    with connection:
                        connection.execute(
                            "UPDATE transient_message_cleanup SET due_at=? WHERE chat_id=? AND message_id=?",
                            (time.time() + 60, chat_id, message_id),
                        )
                await asyncio.sleep(60)
                continue
            with closing(history_connection()) as connection:
                with connection:
                    connection.execute(
                        "DELETE FROM transient_message_cleanup WHERE chat_id=? AND message_id=?",
                        (chat_id, message_id),
                    )
            break


def resume_transient_cleanup(bot) -> None:
    with closing(history_connection()) as connection:
        with connection:
            connection.execute(
                "UPDATE transient_message_cleanup SET active=0, due_at=? WHERE active=1",
                (time.time(),),
            )
            rows = connection.execute(
                "SELECT chat_id, message_id, due_at FROM transient_message_cleanup"
            ).fetchall()
    for chat_id, message_id, due_at in rows:
        asyncio.create_task(delete_transient_messages(
            bot, chat_id, (message_id,), max(0, due_at - time.time()),
        ))


async def rename_topic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user
    response = None
    if chat.type != "private" or user is None or user.id != chat.id:
        response = "Переименовать тему можно только в своём чате с ботом."
    else:
        thread_id = getattr(message, "message_thread_id", None)
        name = " ".join(context.args or []).strip()
        if thread_id is None:
            response = "Откройте нужную тему и отправьте /renametopic Новое название."
        elif not name or len(name) > 128 or any(ord(char) < 32 for char in name):
            response = "Укажите новое название темы длиной от 1 до 128 символов."
        else:
            try:
                await context.bot.edit_forum_topic(
                    chat_id=chat.id, message_thread_id=thread_id, name=name,
                )
            except TelegramError as exc:
                LOG.warning("Could not rename topic %s in chat %s: %s", thread_id, chat.id, exc)
                response = "Не удалось переименовать тему. Проверьте, что она ещё существует."
            else:
                response = f"Тема переименована: {name}"
    reply = await message.reply_text(response)
    schedule_transient_cleanup(
        context.bot, chat.id, (message.message_id, reply.message_id), TRANSIENT_REPLY_SECONDS,
    )


def shared_artist_view(token: str, groups: list[tuple[str, list[dict]]], page: int):
    if not groups:
        return "В этой подборке пока нет доступных MP3.", None
    page_count = (len(groups) - 1) // PLAYLIST_PAGE_SIZE + 1
    page = min(max(page, 0), page_count - 1)
    start_index = page * PLAYLIST_PAGE_SIZE
    rows = [
        [InlineKeyboardButton(
            f"🎤 {artist} ({len(tracks)})"[:64],
            callback_data=f"shr:{token}:g:{index}:0",
        )]
        for index, (artist, tracks) in enumerate(
            groups[start_index:start_index + PLAYLIST_PAGE_SIZE], start_index
        )
    ]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("◀️", callback_data=f"shr:{token}:a:{page - 1}"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("▶️", callback_data=f"shr:{token}:a:{page + 1}"))
    if navigation:
        rows.append(navigation)
    return f"🎧 Подборка · исполнители {page + 1}/{page_count}", InlineKeyboardMarkup(rows)


def shared_track_view(token: str, groups: list[tuple[str, list[dict]]], artist_index: int, page: int):
    artist, tracks = groups[artist_index]
    page_count = (len(tracks) - 1) // PLAYLIST_PAGE_SIZE + 1
    page = min(max(page, 0), page_count - 1)
    start_index = page * PLAYLIST_PAGE_SIZE
    rows = [
        [InlineKeyboardButton(
            f"🎵 {track['title']}"[:64],
            callback_data=f"shr:{token}:s:{track['video_id']}",
        )]
        for track in tracks[start_index:start_index + PLAYLIST_PAGE_SIZE]
    ]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            "◀️", callback_data=f"shr:{token}:g:{artist_index}:{page - 1}"
        ))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton(
            "▶️", callback_data=f"shr:{token}:g:{artist_index}:{page + 1}"
        ))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(
        "◀️ К исполнителям", callback_data=f"shr:{token}:a:{artist_index // PLAYLIST_PAGE_SIZE}",
    )])
    return f"🎤 {artist} · песни {page + 1}/{page_count}", InlineKeyboardMarkup(rows)


async def open_shared_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Откройте подборку в личном чате с ботом.")
        return
    try:
        owner = share_owner(token)
        if owner is None:
            await update.effective_message.reply_text("Ссылка на подборку недействительна или отозвана.")
            return
        lock = context.application.bot_data.setdefault(f"share_access_lock:{owner}", asyncio.Lock())
        async with lock:
            if share_owner(token) != owner:
                await update.effective_message.reply_text("Ссылка на подборку недействительна или отозвана.")
                return
            if update.effective_chat.id == owner:
                await update.effective_message.reply_text("Это ваша подборка. Сохранённые песни уже есть в этом чате.")
                return
            me = await context.bot.get_me()
            if not getattr(me, "has_topics_enabled", False):
                text, markup = shared_artist_view(token, shared_groups(owner), 0)
                if markup is not None:
                    markup = InlineKeyboardMarkup([*markup.inline_keyboard, *zip_markup(token).inline_keyboard])
                else:
                    markup = zip_markup(token)
                await update.effective_message.reply_text(text, reply_markup=markup)
                return
            recipient = update.effective_chat.id
            task_key = (owner, recipient)
            active = context.application.bot_data.setdefault("shared_sync_tasks", {})
            if task_key in active and not active[task_key].done():
                await update.effective_message.reply_text("Темы уже заполняются. Дождитесь завершения.")
                return
            save_shared_subscription(owner, recipient, token, None)
            status = await update.effective_message.reply_text(
                "Создаю темы и переношу сохранённые MP3. Это может занять несколько минут.",
                reply_markup=zip_markup(token),
            )
            status_id = getattr(status, "message_id", None)
            if not isinstance(status_id, int):
                status_id = None
            save_shared_subscription(owner, recipient, token, status_id)
            context.application.bot_data[f"shared_next_message_at:{recipient}"] = (
                asyncio.get_running_loop().time() + SHARE_SEND_INTERVAL
            )
            schedule_shared_topic_sync(context.application, owner, recipient, token, status_id, update)
    except (TelegramError, sqlite3.Error, OSError) as exc:
        LOG.warning("Could not open shared playlist: %s", exc)
        await update.effective_message.reply_text("Не удалось открыть подборку. Попробуйте позже.")
        return


class SharedLinkRevoked(Exception):
    """The owner revoked the bearer link while a recipient sync was running."""


def schedule_shared_topic_sync(
    app: Application, owner: int, recipient: int, token: str,
    status_message_id: int | None, update: Update | None = None,
) -> bool:
    active = app.bot_data.setdefault("shared_sync_tasks", {})
    key = (owner, recipient)
    if key in active and not active[key].done():
        return False
    active[key] = app.create_task(
        sync_shared_topics(app, owner, recipient, token, status_message_id), update=update
    )
    return True


async def report_shared_sync(
    app: Application, recipient: int, message_id: int | None, text: str, token: str | None = None,
) -> None:
    if message_id is None:
        return
    try:
        await app.bot.edit_message_text(
            chat_id=recipient, message_id=message_id, text=text,
            reply_markup=zip_markup(token) if token and share_owner(token) is not None else None,
        )
    except TelegramError as exc:
        LOG.warning("Could not update shared playlist progress: %s", exc)


async def shared_chat_call(app: Application, owner: int, recipient: int, token: str, action):
    """Pace topic messages per chat, retry flood limits, and recheck access before each call."""
    delivery_lock = app.bot_data.setdefault(f"shared_delivery_lock:{recipient}", asyncio.Lock())
    access_lock = app.bot_data.setdefault(f"share_access_lock:{owner}", asyncio.Lock())
    async with delivery_lock:
        loop = asyncio.get_running_loop()
        next_key = f"shared_next_message_at:{recipient}"
        delay = app.bot_data.get(next_key, 0) - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        for attempt in range(3):
            async with access_lock:
                if share_owner(token) != owner:
                    raise SharedLinkRevoked
                try:
                    result = await action()
                except RetryAfter as exc:
                    retry_after = exc.retry_after
                else:
                    app.bot_data[next_key] = loop.time() + SHARE_SEND_INTERVAL
                    return result
                app.bot_data[next_key] = loop.time() + SHARE_SEND_INTERVAL
            if attempt == 2:
                raise RetryAfter(retry_after)
            wait = retry_after.total_seconds() if hasattr(retry_after, "total_seconds") else float(retry_after)
            await asyncio.sleep(max(wait, SHARE_SEND_INTERVAL))
    raise RuntimeError("Shared chat call exhausted retries")


async def sync_shared_topics(
    app: Application, owner: int, recipient: int, token: str, status_message_id: int | None,
) -> None:
    key = (owner, recipient)
    sync_lock = app.bot_data.setdefault(f"shared_sync_lock:{owner}:{recipient}", asyncio.Lock())
    copied = skipped = failed = moved = 0
    try:
        async with sync_lock:
            if share_owner(token) != owner:
                raise SharedLinkRevoked
            groups = shared_topic_groups(owner)
            total = sum(len(tracks) for _, _, tracks in groups)
            if total == 0:
                set_shared_subscription_state(owner, recipient, token, False, None)
                await report_shared_sync(app, recipient, status_message_id, "В подборке пока нет доступных MP3.", token)
                return
            for topic_key, topic_name, tracks in groups:
                if share_owner(token) != owner:
                    raise SharedLinkRevoked
                thread = shared_recipient_topic(owner, recipient, topic_key)
                if thread is None:
                    async def create_topic():
                        topic = await app.bot.create_forum_topic(chat_id=recipient, name=topic_name)
                        record_shared_recipient_topic(owner, recipient, topic_key, topic.message_thread_id)
                        return topic.message_thread_id
                    try:
                        thread = await shared_chat_call(app, owner, recipient, token, create_topic)
                    except SharedLinkRevoked:
                        raise
                    except (TelegramError, sqlite3.Error, OSError) as exc:
                        LOG.warning("Could not create shared topic %s: %s", topic_name, exc)
                        failed += len(tracks)
                        continue
                track_index = 0
                repaired_topic = False
                while track_index < len(tracks):
                    track = tracks[track_index]
                    if share_owner(token) != owner:
                        raise SharedLinkRevoked
                    track_key = track["song_key"] or track["video_id"]
                    previous = shared_recipient_track(owner, recipient, track_key)
                    if previous and previous[0] == topic_key:
                        skipped += 1
                        track_index += 1
                        continue
                    async def send_track():
                        sent = await app.bot.send_audio(
                            chat_id=recipient, message_thread_id=thread,
                            audio=track["audio_file_id"], performer=track["artist"], title=track["title"],
                        )
                        record_shared_recipient_track(
                            owner, recipient, track_key, track["video_id"], topic_key, sent.message_id
                        )
                        if previous:
                            try:
                                await app.bot.delete_message(chat_id=recipient, message_id=previous[1])
                            except TelegramError as exc:
                                LOG.warning("Could not remove prior shared track copy: %s", exc)
                        return sent
                    try:
                        await shared_chat_call(app, owner, recipient, token, send_track)
                    except SharedLinkRevoked:
                        raise
                    except BadRequest as exc:
                        message = str(exc).casefold()
                        if not repaired_topic and ("message thread not found" in message or "topic not found" in message):
                            forget_shared_recipient_topic(owner, recipient, topic_key)
                            async def recreate_topic():
                                topic = await app.bot.create_forum_topic(chat_id=recipient, name=topic_name)
                                record_shared_recipient_topic(owner, recipient, topic_key, topic.message_thread_id)
                                return topic.message_thread_id
                            thread = await shared_chat_call(app, owner, recipient, token, recreate_topic)
                            repaired_topic = True
                            track_index = 0
                            continue
                        LOG.warning("Could not copy shared MP3 %s: %s", track["video_id"], exc)
                        failed += 1
                        track_index += 1
                        continue
                    except (TelegramError, sqlite3.Error, OSError) as exc:
                        LOG.warning("Could not copy shared MP3 %s: %s", track["video_id"], exc)
                        failed += 1
                        track_index += 1
                        continue
                    copied += 1
                    moved += bool(previous)
                    track_index += 1
                    if copied % 10 == 0:
                        await report_shared_sync(
                            app, recipient, status_message_id,
                            f"Перенесено {copied}/{total} MP3 · уже были {skipped} · ошибок {failed}.",
                            token,
                        )
            if share_owner(token) != owner:
                raise SharedLinkRevoked
            set_shared_subscription_state(owner, recipient, token, bool(failed),
                                          f"{failed} MP3 не отправлено" if failed else None)
            summary = f"Готово: {copied} новых MP3, {skipped} уже были."
            if moved:
                summary += f" Перемещено по темам: {moved}."
            if failed:
                summary += f" Ошибок: {failed}. Откройте ссылку снова для повтора."
            await report_shared_sync(app, recipient, status_message_id, summary, token)
    except SharedLinkRevoked:
        set_shared_subscription_state(owner, recipient, token, False, "revoked")
        await report_shared_sync(app, recipient, status_message_id, "Ссылка отозвана. Перенос остановлен.")
    except (TelegramError, sqlite3.Error, OSError) as exc:
        LOG.warning("Shared playlist sync stopped: %s", exc)
        set_shared_subscription_state(owner, recipient, token, True, str(exc)[:200])
        await report_shared_sync(app, recipient, status_message_id,
                                 "Перенос прерван. Бот продолжит после перезапуска или открытия ссылки.")
    finally:
        app.bot_data.setdefault("shared_sync_tasks", {}).pop(key, None)


async def resume_pending_shared_syncs(app: Application) -> None:
    """Resume durable recipient copies after polling starts."""
    while not app.running:
        await asyncio.sleep(0.1)
    try:
        subscriptions = pending_shared_subscriptions()
    except (sqlite3.Error, OSError) as exc:
        LOG.warning("Could not resume shared playlists: %s", exc)
        return
    for owner, recipient, token, status_message_id in subscriptions:
        schedule_shared_topic_sync(app, owner, recipient, token, status_message_id)


async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Создать ссылку можно только в личном чате с ботом.")
        return
    try:
        lock = context.application.bot_data.setdefault(
            f"share_access_lock:{update.effective_chat.id}", asyncio.Lock()
        )
        async with lock:
            username = (await context.bot.get_me()).username
            if not username:
                raise ValueError("Bot has no username")
            token = get_or_create_share_token(update.effective_chat.id)
            await update.effective_message.reply_text(
                f"Подборка: https://t.me/{username}?start=share_{token}\n"
                "Любой человек с этой ссылкой сможет слушать ваши сохранённые MP3. "
                "Чтобы закрыть доступ, отправьте /unshare. Уже отправленные копии останутся у получателей.",
                reply_markup=zip_markup(token),
            )
    except (TelegramError, sqlite3.Error, OSError, ValueError) as exc:
        LOG.warning("Could not create shared playlist: %s", exc)
        await update.effective_message.reply_text("Не удалось создать ссылку. Попробуйте позже.")
        return


async def unshare_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Закрыть доступ можно только в личном чате с ботом.")
        return
    try:
        lock = context.application.bot_data.setdefault(
            f"share_access_lock:{update.effective_chat.id}", asyncio.Lock()
        )
        async with lock:
            revoked = revoke_share_token(update.effective_chat.id)
            await update.effective_message.reply_text(
                "Ссылка отозвана. Доступ к подборке закрыт." if revoked else "Активной ссылки нет."
            )
    except (TelegramError, sqlite3.Error, OSError) as exc:
        LOG.warning("Could not revoke shared playlist: %s", exc)
        await update.effective_message.reply_text("Не удалось закрыть доступ. Попробуйте позже.")
        return


def zip_track_name(number: int, track: dict) -> str:
    """Number entries and keep names safe, short and unique on phone filesystems."""
    label = f"{track['artist']} - {track['title']}"
    label = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", label).strip(" .")[:140]
    return f"{number:03d} - {label or 'MP3'}.mp3"


def zip_caption(artist: str, part_number: int, titles: list[str]) -> str:
    caption = f"{artist} · часть {part_number}\n" + "\n".join(titles)
    if len(caption.encode("utf-16-le")) <= 2048:
        return caption
    result = []
    units = 0
    for character in caption:
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > 1023:
            break
        result.append(character)
        units += width
    return "".join(result) + "…"


async def retry_zip_request(action, state: dict | None = None):
    for attempt in range(3):
        if state and state["cancelled"]:
            raise ZipCancelled
        try:
            return await action()
        except RetryAfter as exc:
            failure = exc
            delay = exc.retry_after
            delay = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
        except BadRequest:
            raise
        except NetworkError as exc:
            failure = exc
            delay = 2 ** attempt
        if attempt == 2:
            raise failure
        await asyncio.sleep(max(0, delay))


def zip_stop_markup(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✖️ Остановить ZIP", callback_data=f"zipstop:{job_id}"),
    ]])


async def update_zip_status(
    app: Application, recipient: int, message_id: int, text: str, job_id: str | None = None,
) -> None:
    """Status edits must never interrupt archive construction or delivery."""
    try:
        await retry_zip_request(lambda: app.bot.edit_message_text(
            chat_id=recipient, message_id=message_id, text=text,
            reply_markup=zip_stop_markup(job_id) if job_id else None,
        ))
    except TelegramError as exc:
        LOG.warning("Could not update ZIP progress: %s", exc)


class ZipDeliveryUnknown(Exception):
    def __init__(self, part_number: int):
        super().__init__(f"ZIP part {part_number} may have been delivered")
        self.part_number = part_number


class ZipCancelled(Exception):
    pass


def zip_access_allowed(owner: int, recipient: int, token: str | None) -> bool:
    """Own-library exports need no share link; linked exports still require it."""
    return owner == recipient if token is None else share_owner(token) == owner


async def send_zip_part(
    app: Application, owner: int, recipient: int, token: str | None, path: Path,
    part_number: int, thread_id: int | None, filename: str, caption: str, state: dict,
) -> None:
    if path.stat().st_size >= MAX_FILE_BYTES:
        raise ValueError("ZIP превысил лимит Telegram 50 МБ")
    access_lock = app.bot_data.setdefault(f"share_access_lock:{owner}", asyncio.Lock())
    for attempt in range(3):
        async with access_lock:
            if state["cancelled"]:
                raise ZipCancelled
            if not zip_access_allowed(owner, recipient, token):
                raise SharedLinkRevoked
            try:
                with path.open("rb") as document:
                    await app.bot.send_document(
                        chat_id=recipient, document=document,
                        filename=filename,
                        caption=caption,
                        message_thread_id=thread_id,
                        read_timeout=180, write_timeout=180, connect_timeout=30, pool_timeout=30,
                    )
                return
            except RetryAfter as exc:
                failure = exc
                delay = exc.retry_after
                delay = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
            except BadRequest:
                raise
            except NetworkError as exc:
                raise ZipDeliveryUnknown(part_number) from exc
        if attempt == 2:
            raise failure
        await asyncio.sleep(max(0, delay))


async def export_shared_zip(
    app: Application, owner: int, recipient: int, token: str | None, thread_id: int | None,
    state: dict,
) -> None:
    active = app.bot_data.setdefault("zip_archive_tasks", {})
    gate = app.bot_data.setdefault("zip_archive_gate", asyncio.Semaphore(1))
    status_id = None
    status_retention = ZIP_ERROR_STATUS_SECONDS
    failures: list[str] = []
    sent_tracks_count = sent_parts = 0
    total = 0
    try:
        status = await retry_zip_request(lambda: app.bot.send_message(
            chat_id=recipient, text="ZIP поставлен в очередь. Бот сообщит о ходе отправки.",
            message_thread_id=thread_id,
            reply_markup=zip_stop_markup(state["job_id"]),
        ))
        status_id = status.message_id
        state["status_id"] = status_id
        queue_transient_cleanup(recipient, (status_id,), 0, active=True)
        async with gate:
            if state["cancelled"]:
                raise ZipCancelled
            if not zip_access_allowed(owner, recipient, token):
                raise SharedLinkRevoked
            groups = sorted(shared_groups(owner), key=lambda group: (-len(group[1]), group[0].casefold()))
            tracks = [(artist, track) for artist, group in groups for track in group]
            total = len(tracks)
            await update_zip_status(
                app, recipient, status_id,
                text=f"Готовлю {total} MP3 в отдельных ZIP частях до 45 МБ. "
                     "Если бот перезапустится, нажмите кнопку ещё раз.",
                job_id=state["job_id"],
            )
            if not tracks:
                await update_zip_status(app, recipient, status_id, "В подборке пока нет доступных MP3.")
                return
            with tempfile.TemporaryDirectory(prefix="telega-zip-") as temporary:
                directory = Path(temporary)
                part_number = 1
                path = directory / f"music-part-{part_number:03d}.zip"
                archive = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=False)
                entries: list[str] = []
                titles: list[str] = []
                part_artist = ""

                async def flush_part() -> None:
                    nonlocal sent_tracks_count, sent_parts
                    archive.close()
                    if not entries:
                        path.unlink(missing_ok=True)
                        return
                    if state["cancelled"]:
                        raise ZipCancelled
                    safe_artist = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", part_artist).strip(" .")[:60] or "Artist"
                    filename = f"{safe_artist}-part-{part_number:03d}.zip"
                    caption = zip_caption(part_artist, part_number, titles)
                    try:
                        await send_zip_part(
                            app, owner, recipient, token, path, part_number, thread_id,
                            filename, caption, state,
                        )
                    except SharedLinkRevoked:
                        raise
                    except ZipDeliveryUnknown:
                        raise
                    except (TelegramError, OSError, ValueError) as exc:
                        LOG.warning("Could not send ZIP part %s: %s", part_number, exc)
                        failures.extend(f"{name}: часть ZIP не отправлена" for name in entries)
                    else:
                        sent_tracks_count += len(entries)
                        sent_parts += 1
                    finally:
                        path.unlink(missing_ok=True)
                    await update_zip_status(
                        app, recipient, status_id,
                        f"ZIP: отправлено {sent_tracks_count}/{total} MP3 в {sent_parts} частях; "
                        f"ошибок {len(failures)}.",
                        job_id=state["job_id"],
                    )

                try:
                    for number, (artist, track) in enumerate(tracks, 1):
                        if state["cancelled"]:
                            raise ZipCancelled
                        if not zip_access_allowed(owner, recipient, token):
                            raise SharedLinkRevoked
                        if entries and artist != part_artist:
                            await flush_part()
                            part_number += 1
                            path = directory / f"music-part-{part_number:03d}.zip"
                            archive = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=False)
                            entries = []
                            titles = []
                        name = zip_track_name(number, track)
                        mp3_path = directory / "current.mp3"
                        try:
                            telegram_file = await retry_zip_request(
                                lambda: app.bot.get_file(track["audio_file_id"], read_timeout=120), state,
                            )
                            if telegram_file.file_size and telegram_file.file_size > ZIP_FETCH_LIMIT_BYTES:
                                raise ValueError("MP3 больше лимита getFile 20 МБ")
                            async def fetch_file():
                                mp3_path.unlink(missing_ok=True)
                                return await telegram_file.download_to_drive(
                                    custom_path=mp3_path, read_timeout=180, connect_timeout=30,
                                )
                            await retry_zip_request(fetch_file, state)
                            if state["cancelled"]:
                                raise ZipCancelled
                            size = mp3_path.stat().st_size
                            if size > ZIP_FETCH_LIMIT_BYTES:
                                raise ValueError("MP3 больше лимита getFile 20 МБ")
                            if not size:
                                raise ValueError("пустой MP3")
                        except (TelegramError, OSError, ValueError) as exc:
                            LOG.warning("Could not fetch shared MP3 %s for ZIP: %s", track["video_id"], exc)
                            if isinstance(exc, ValueError) and str(exc) == "MP3 больше лимита getFile 20 МБ":
                                reason = "MP3 больше лимита getFile 20 МБ"
                            elif isinstance(exc, BadRequest) and "file is too big" in str(exc).casefold():
                                reason = "MP3 больше лимита getFile 20 МБ"
                            elif isinstance(exc, BadRequest):
                                reason = "Telegram не дал получить MP3"
                            elif isinstance(exc, RetryAfter):
                                reason = "Telegram временно ограничил запросы"
                            elif isinstance(exc, NetworkError):
                                reason = "временная ошибка Telegram после 3 попыток"
                            elif isinstance(exc, TelegramError):
                                reason = "Telegram не дал получить MP3"
                            elif isinstance(exc, OSError):
                                reason = "ошибка сохранения MP3 на сервере"
                            else:
                                reason = "ошибка размера или содержимого MP3"
                            failures.append(f"{name}: {reason}")
                            continue
                        try:
                            reserve = (len(entries) + 2) * 1024
                            if entries and path.stat().st_size + size + reserve > ZIP_PART_TARGET_BYTES:
                                await flush_part()
                                part_number += 1
                                path = directory / f"music-part-{part_number:03d}.zip"
                                archive = zipfile.ZipFile(
                                    path, "w", compression=zipfile.ZIP_STORED, allowZip64=False
                                )
                                entries = []
                                titles = []
                            part_artist = artist
                            await asyncio.to_thread(archive.write, mp3_path, arcname=name)
                            entries.append(name)
                            titles.append(track["title"])
                        finally:
                            mp3_path.unlink(missing_ok=True)
                    await flush_part()
                finally:
                    archive.close()
            if state["cancelled"]:
                raise ZipCancelled
            if not zip_access_allowed(owner, recipient, token):
                raise SharedLinkRevoked
            summary = f"ZIP готов: {sent_tracks_count}/{total} MP3 в {sent_parts} частях."
            if failures:
                summary += f" Не удалось включить {len(failures)} MP3."
                summary += " Пропущено: " + "; ".join(failures[:3])[:1500]
                if len(failures) > 3:
                    summary += " Подробный список ниже."
            await update_zip_status(app, recipient, status_id, summary)
            if not failures:
                status_retention = ZIP_SUCCESS_STATUS_SECONDS
            for offset in range(0, len(failures), 25):
                try:
                    failure_notice = await retry_zip_request(lambda: app.bot.send_message(
                        chat_id=recipient, message_thread_id=thread_id,
                        text="Не вошли в ZIP:\n" + "\n".join(failures[offset:offset + 25])[:3900],
                    ))
                    schedule_transient_cleanup(
                        app.bot, recipient, (failure_notice.message_id,), ZIP_ERROR_STATUS_SECONDS,
                    )
                except TelegramError as exc:
                    LOG.warning("Could not send ZIP failure list: %s", exc)
    except SharedLinkRevoked:
        if status_id is not None:
            await update_zip_status(
                app, recipient, status_id,
                f"Ссылка отозвана. ZIP остановлен: отправлено {sent_tracks_count}/{total} MP3.",
            )
    except ZipCancelled:
        if status_id is not None:
            await update_zip_status(
                app, recipient, status_id,
                f"ZIP остановлен: отправлено {sent_tracks_count}/{total} MP3 в {sent_parts} частях. "
                "Уже отправленные части остались в чате.",
            )
            status_retention = ZIP_SUCCESS_STATUS_SECONDS
    except ZipDeliveryUnknown as exc:
        LOG.warning("ZIP upload outcome unknown for part %s", exc.part_number)
        if status_id is not None:
            await update_zip_status(
                app, recipient, status_id,
                f"Отправка ZIP остановлена на части {exc.part_number}: часть могла дойти. "
                "Проверьте чат перед повторным нажатием кнопки. Полная доставка не подтверждена.",
            )
    except (TelegramError, OSError, ValueError, zipfile.BadZipFile) as exc:
        LOG.warning("ZIP export stopped: %s", exc)
        if status_id is not None:
            try:
                await app.bot.edit_message_text(
                    chat_id=recipient, message_id=status_id,
                    text="ZIP прерван. Нажмите «Скачать всё ZIP» ещё раз; уже отправленные части останутся в чате.",
                )
            except TelegramError:
                pass
    finally:
        if status_id is not None:
            schedule_transient_cleanup(app.bot, recipient, (status_id,), status_retention)
        active.pop(recipient, None)
        if app.bot_data.setdefault("zip_archive_states", {}).get(recipient) is state:
            app.bot_data["zip_archive_states"].pop(recipient, None)


async def zip_archive_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.message.chat.type != "private":
        await query.answer("Откройте подборку в личном чате.", show_alert=True)
        return
    recipient = query.message.chat_id
    token = None if query.data == "zip:own" else query.data[4:]
    if token is None:
        if query.from_user.id != recipient:
            await query.answer("Это чужая библиотека.", show_alert=True)
            return
        owner = recipient
    else:
        owner = share_owner(token)
        if owner is None:
            await query.answer("Ссылка больше не действует.", show_alert=True)
            return
    if not queue_zip_archive(
        context.application, update, owner, recipient, token,
        getattr(query.message, "message_thread_id", None),
    ):
        await query.answer("ZIP уже готовится. Дождитесь завершения.", show_alert=True)
        return
    await query.answer("Готовлю ZIP. Отправлю файлы сюда.")


async def stop_zip_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    recipient = query.message.chat_id
    state = context.application.bot_data.setdefault("zip_archive_states", {}).get(recipient)
    if (query.message.chat.type != "private" or query.from_user.id != recipient
            or not state or state["job_id"] != query.data.partition(":")[2]
            or state["status_id"] != query.message.message_id):
        await query.answer("Эта кнопка больше недоступна.", show_alert=True)
        return
    state["cancelled"] = True
    await query.answer("Останавливаю ZIP после текущего действия.")


def queue_zip_archive(
    app: Application, update: Update, owner: int, recipient: int,
    token: str | None, thread_id: int | None,
) -> bool:
    active = app.bot_data.setdefault("zip_archive_tasks", {})
    if recipient in active and not active[recipient].done():
        return False
    state = {"job_id": uuid4().hex[:12], "cancelled": False, "status_id": None}
    app.bot_data.setdefault("zip_archive_states", {})[recipient] = state
    active[recipient] = app.create_task(
        export_shared_zip(app, owner, recipient, token, thread_id, state), update=update,
    )
    return True


async def zip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type != "private":
        await update.effective_message.reply_text("Скачать ZIP можно только в личном чате с ботом.")
        return
    if update.effective_user.id != chat.id:
        await update.effective_message.reply_text("Это чужая библиотека.")
        return
    if queue_zip_archive(
        context.application, update, chat.id, chat.id, None,
        getattr(update.effective_message, "message_thread_id", None),
    ):
        schedule_transient_cleanup(
            context.bot, chat.id, (update.effective_message.message_id,), 0,
        )
    else:
        reply = await update.effective_message.reply_text("ZIP уже готовится. Дождитесь завершения.")
        schedule_transient_cleanup(
            context.bot, chat.id, (update.effective_message.message_id, reply.message_id),
            TRANSIENT_REPLY_SECONDS,
        )


async def shared_playlist_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.message.chat.type != "private":
        await query.answer("Откройте подборку в личном чате.", show_alert=True)
        return
    _, token, action, *parts = query.data.split(":")
    try:
        owner = share_owner(token)
        if owner is None:
            await query.answer("Ссылка больше не действует.", show_alert=True)
            return
        lock = context.application.bot_data.setdefault(
            f"share_access_lock:{owner}", asyncio.Lock()
        )
        async with lock:
            if share_owner(token) != owner:
                await query.answer("Ссылка больше не действует.", show_alert=True)
                return
            groups = shared_groups(owner)
            if action == "s":
                video_id = parts[0]
                track = next((
                    track for _, tracks in groups for track in tracks
                    if track["video_id"] == video_id
                ), None)
                if track is None:
                    await query.answer("Эта песня больше недоступна.", show_alert=True)
                    return
                await query.answer("Отправляю MP3…")
                target = {"chat_id": query.message.chat_id, "audio": track["audio_file_id"],
                          "performer": track["artist"], "title": track["title"]}
                thread = getattr(query.message, "message_thread_id", None)
                if thread is not None:
                    target["message_thread_id"] = thread
                try:
                    await context.bot.send_audio(**target)
                except TelegramError as exc:
                    LOG.warning("Could not send shared MP3: %s", exc)
                    await query.message.reply_text("Не удалось отправить MP3. Попробуйте позже.")
                return
            if action == "a":
                text, markup = shared_artist_view(token, groups, int(parts[0]))
            else:
                artist_index = int(parts[0])
                if artist_index >= len(groups):
                    await query.answer("Список изменился. Откройте ссылку заново.", show_alert=True)
                    return
                text, markup = shared_track_view(token, groups, artist_index, int(parts[1]))
            await query.answer()
            await query.edit_message_text(text, reply_markup=markup)
    except (TelegramError, sqlite3.Error, OSError) as exc:
        LOG.warning("Could not show shared playlist: %s", exc)
        await query.answer("Не удалось открыть песню. Попробуйте позже.", show_alert=True)
        return


def playlist_markup(job_id: str, job: dict) -> InlineKeyboardMarkup:
    entries = job["entries"]
    page = job["page"]
    selected = job["selected"]
    start = page * PLAYLIST_PAGE_SIZE
    rows = []
    for index in range(start, min(start + PLAYLIST_PAGE_SIZE, len(entries))):
        label = entries[index][1].replace("\n", " ").strip()
        if len(label) > 48:
            label = label[:47] + "…"
        mark = "✅" if index in selected else "⬜"
        rows.append([InlineKeyboardButton(
            f"{mark} {index + 1}. {label}", callback_data=f"mix:{job_id}:toggle:{index}"
        )])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("◀️", callback_data=f"mix:{job_id}:page:{page - 1}"))
    navigation.append(InlineKeyboardButton(
        f"{page + 1}/{max(1, (len(entries) - 1) // PLAYLIST_PAGE_SIZE + 1)}",
        callback_data=f"mix:{job_id}:page:{page}",
    ))
    if start + PLAYLIST_PAGE_SIZE < len(entries):
        navigation.append(InlineKeyboardButton("▶️", callback_data=f"mix:{job_id}:page:{page + 1}"))
    rows.append(navigation)
    if job.get("has_more"):
        rows.append([InlineKeyboardButton(
            "📃 Следующие 30", callback_data=f"mix:{job_id}:more:next"
        )])
    if job.get("reasons"):
        rows.append([InlineKeyboardButton(
            f"🔎 Почему скрыты ({len(job['reasons'])})",
            callback_data=f"mix:{job_id}:why:show",
        )])
    if entries or job.get("has_more"):
        rows.append([InlineKeyboardButton(
            "⚡ Скачать 30 новых MP3", callback_data=f"mix:{job_id}:auto:mp3",
        )])
    if entries:
        rows.append([
            InlineKeyboardButton("✅ Выбрать все", callback_data=f"mix:{job_id}:select:all"),
            InlineKeyboardButton("☐ Снять выбор", callback_data=f"mix:{job_id}:select:none"),
        ])
        rows.append([InlineKeyboardButton(
            f"📌 Уже есть в чате ({len(selected)})", callback_data=f"mix:{job_id}:remember:now"
        )])
        rows.append([
            InlineKeyboardButton(f"🎵 MP3 ({len(selected)})", callback_data=f"mix:{job_id}:download:mp3"),
            InlineKeyboardButton(f"🎬 MP4 ({len(selected)})", callback_data=f"mix:{job_id}:download:mp4"),
        ])
    rows.append([InlineKeyboardButton("✖️ Закрыть список", callback_data=f"mix:{job_id}:close:now")])
    return InlineKeyboardMarkup(rows)


def playlist_text(job: dict) -> str:
    instruction = (
        "Нажмите на треки, затем выберите формат." if job["entries"] else
        "Новых треков на этой странице нет. Посмотрите причины или откройте следующие 30."
    )
    text = (
        f"{job['title']}\n"
        f"Треков в списке: {len(job['entries'])}. Выбрано: {len(job['selected'])}/{len(job['entries'])}.\n"
        f"{instruction}"
    )
    if job.get("skipped"):
        text += f"\nУже отправлены в этот чат и скрыты: {job['skipped']}."
    if len(job["entries"]) >= MAX_PLAYLIST_SELECTION:
        text += f"\nЗа одну загрузку можно выбрать до {MAX_PLAYLIST_SELECTION} треков."
    if job.get("page_error"):
        text += "\nСледующую страницу пока не удалось получить. Попробуйте ещё раз."
    if job.get("auto_notice"):
        text += f"\n{job['auto_notice']}"
    return text


async def collect_auto_playlist(job: dict) -> tuple[list[tuple[str, str, str]], int]:
    """Find up to 30 unsent tracks while inspecting no more than 150 Mix positions."""
    chat_id = job["source_chat_id"]
    entries = job["entries"]
    start = job["next_start"]
    has_more = job["has_more"]
    if start > AUTO_PLAYLIST_SCAN_LIMIT + 1:
        entries, start, has_more = [], 1, True
    chosen = []
    seen_ids = set()
    seen_keys = set()

    def add_new(page_entries: list[tuple[str, str, str]]) -> None:
        available, _ = filter_sent_tracks_explained(chat_id, page_entries)
        for entry in available:
            video_id = parse_qs(urlsplit(entry[0]).query)["v"][0]
            key = entry[2]
            if video_id in seen_ids or (key and key in seen_keys):
                continue
            seen_ids.add(video_id)
            if key:
                seen_keys.add(key)
            chosen.append(entry)
            if len(chosen) >= AUTO_PLAYLIST_DOWNLOAD_LIMIT:
                break

    add_new(entries)
    while len(chosen) < AUTO_PLAYLIST_DOWNLOAD_LIMIT and has_more and start <= AUTO_PLAYLIST_SCAN_LIMIT:
        _, page_entries, has_more = await asyncio.to_thread(list_playlist, job["url"], start)
        add_new(page_entries)
        start += PLAYLIST_LIMIT
    return chosen, min(start - 1, AUTO_PLAYLIST_SCAN_LIMIT)


def format_markup(job_id: str, *, dismiss: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("🎵 MP3", callback_data=f"mp3:{job_id}"),
        InlineKeyboardButton("🎬 MP4", callback_data=f"mp4:{job_id}"),
    ]]
    if dismiss:
        rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data=f"format:{job_id}:close")])
    return InlineKeyboardMarkup(rows)


def quality_markup(job_id: str, media_type: str, *, mix: bool = False, dismiss: bool = False) -> InlineKeyboardMarkup:
    unit = "кбит/с" if media_type == "mp3" else "p"
    prefix = f"mix:{job_id}:quality:{media_type}:" if mix else f"quality:{job_id}:{media_type}:"
    rows = [[InlineKeyboardButton(
        f"{quality} {unit}", callback_data=f"{prefix}{quality}"
    )] for quality in QUALITY_OPTIONS[media_type]]
    if mix:
        rows.append([InlineKeyboardButton("◀️ К списку", callback_data=f"mix:{job_id}:quality:back")])
    else:
        rows.append([InlineKeyboardButton("◀️ К форматам", callback_data=f"format:{job_id}:back")])
        if dismiss:
            rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data=f"format:{job_id}:close")])
    return InlineKeyboardMarkup(rows)


def hidden_reasons_text(job: dict) -> str:
    reasons = job.get("reasons", [])
    lines = [f"🔎 Скрыто из Mix: {len(reasons)}", "Совпадения:"]
    for index, (incoming, existing, source) in enumerate(reasons[:15], 1):
        lines.append(f"{index}. {incoming[:90]}\n   ↳ {existing[:90]} ({source})")
    if len(reasons) > 15:
        lines.append(f"Показаны первые 15 из {len(reasons)}.")
    return "\n".join(lines)[:4000]


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    raw_urls = [raw.rstrip(".,!?;:)]}") for raw in URL.findall(message.text or "")]
    playlist_urls = [url for raw in raw_urls if (url := normalize_playlist_url(raw))]
    if len(raw_urls) == 1 and playlist_urls:
        status = await message.reply_text("Получаю список треков…")
        try:
            title, entries, has_more = await asyncio.to_thread(list_playlist, playlist_urls[0])
            entries, reasons = filter_sent_tracks_explained(message.chat_id, entries)
        except (DownloadError, ValueError, OSError, sqlite3.Error) as exc:
            LOG.warning("Could not list playlist: %s", exc)
            await status.edit_text("Не удалось получить список треков. Попробуйте другую ссылку на Mix.")
            return
        if not entries and not has_more and not reasons:
            await status.edit_text("Все доступные треки из этого Mix уже отправлены в этот чат.")
            return
        jobs = context.user_data.setdefault("mixes", {})
        if len(jobs) >= 10:
            jobs.pop(next(iter(jobs)))
        job_id = uuid4().hex[:12]
        job = {
            "title": title,
            "url": playlist_urls[0],
            "entries": entries,
            "selected": set(),
            "page": 0,
            "skipped": len(reasons),
            "reasons": reasons,
            "has_more": has_more,
            "next_start": 1 + PLAYLIST_LIMIT,
            "source_chat_id": message.chat_id,
            "source_message_id": message.message_id,
        }
        jobs[job_id] = job
        await status.edit_text(playlist_text(job), reply_markup=playlist_markup(job_id, job))
        return

    urls, invalid = extract_urls(message.text or "")
    if not urls:
        await message.reply_text("Пришлите ссылки на видео YouTube (youtube.com или youtu.be).")
        return
    if len(urls) > MAX_BATCH:
        await message.reply_text(f"За один раз можно отправить до {MAX_BATCH} ссылок. Разделите их на несколько сообщений.")
        return

    jobs = context.user_data.setdefault("jobs", {})
    if len(jobs) >= 10:
        jobs.pop(next(iter(jobs)))
    job_id = uuid4().hex[:12]
    jobs[job_id] = (urls, message.chat_id, message.message_id)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 MP3", callback_data=f"mp3:{job_id}"),
        InlineKeyboardButton("🎬 MP4", callback_data=f"mp4:{job_id}"),
    ]])
    note = f" Пропущено некорректных ссылок: {invalid}." if invalid else ""
    await message.reply_text(f"Принято ссылок: {len(urls)}. Выберите формат для всех:{note}", reply_markup=keyboard)


async def select_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, job_id, action, value = query.data.split(":", 3)
    jobs = context.user_data.setdefault("mixes", {})
    job = jobs.get(job_id)
    if job is None:
        await query.answer("Список устарел. Пришлите ссылку снова.", show_alert=True)
        return
    if job.get("auto_loading"):
        await query.answer("Поиск новых песен уже идёт.")
        return

    if action == "auto":
        job["auto_loading"] = True
        try:
            await query.answer("Ищу новые песни…")
            await query.edit_message_text("Проверяю Mix и ищу новые песни…")
            selected_entries, checked = await collect_auto_playlist(job)
        except (DownloadError, ValueError, OSError, sqlite3.Error) as exc:
            LOG.warning("Could not collect new playlist tracks: %s", exc)
            job["auto_notice"] = "Не удалось проверить весь Mix. Попробуйте ещё раз."
            await query.edit_message_text(playlist_text(job), reply_markup=playlist_markup(job_id, job))
            return
        finally:
            job["auto_loading"] = False
        if not selected_entries:
            job["auto_notice"] = f"Среди первых {checked} позиций новых песен нет."
            await query.edit_message_text(playlist_text(job), reply_markup=playlist_markup(job_id, job))
            return
        jobs.pop(job_id, None)
        urls = [entry[0] for entry in selected_entries]
        known_keys = {entry[0]: entry[2] for entry in selected_entries}
        queue_delivery(
            update, query, context, urls, "mp3", job["source_chat_id"], job["source_message_id"],
            known_keys, job_id=job_id, quality=DEFAULT_QUALITY["mp3"],
        )
        return

    if action == "why":
        await query.answer()
        if value == "show":
            await query.edit_message_text(
                hidden_reasons_text(job), reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ К списку", callback_data=f"mix:{job_id}:why:back")
                ]]),
            )
        else:
            await query.edit_message_text(playlist_text(job), reply_markup=playlist_markup(job_id, job))
        return
    if action == "close":
        jobs.pop(job_id, None)
        await query.answer()
        await delete_batch_messages(
            context, job["source_chat_id"], job["source_message_id"], query.message.message_id,
        )
        return

    if action == "toggle":
        index = int(value)
        if index >= len(job["entries"]):
            await query.answer()
            return
        if index in job["selected"]:
            job["selected"].remove(index)
        elif len(job["selected"]) >= MAX_PLAYLIST_SELECTION:
            await query.answer(f"Можно выбрать до {MAX_PLAYLIST_SELECTION} треков.", show_alert=True)
            return
        else:
            job["selected"].add(index)
    elif action == "page":
        page = int(value)
        if page < 0 or page * PLAYLIST_PAGE_SIZE >= len(job["entries"]):
            await query.answer()
            return
        if page == job["page"]:
            await query.answer()
            return
        job["page"] = page
    elif action == "select":
        selected = set(range(min(len(job["entries"]), MAX_PLAYLIST_SELECTION))) if value == "all" else set()
        if selected == job["selected"]:
            await query.answer("Выбор уже установлен.")
            return
        job["selected"] = selected
    elif action == "more":
        if not job.get("has_more") or job.get("loading"):
            await query.answer("Следующей страницы пока нет.")
            return
        job["loading"] = True
        await query.answer("Загружаю следующие треки…")
        try:
            _, new_entries, has_more = await asyncio.to_thread(
                list_playlist, job["url"], job["next_start"]
            )
            new_entries, reasons = filter_sent_tracks_explained(job["source_chat_id"], new_entries)
            known_ids = {parse_qs(urlsplit(entry[0]).query)["v"][0]: entry[1] for entry in job["entries"]}
            known_song_keys = {entry[2]: entry[1] for entry in job["entries"] if entry[2]}
            unique_entries = []
            for entry in new_entries:
                video_id = parse_qs(urlsplit(entry[0]).query)["v"][0]
                match = known_ids.get(video_id) or (known_song_keys.get(entry[2]) if entry[2] else None)
                if match:
                    reasons.append((entry[1], match, "в этом списке"))
                else:
                    unique_entries.append(entry)
                    known_ids[video_id] = entry[1]
                    if entry[2]:
                        known_song_keys[entry[2]] = entry[1]
            old_count = len(job["entries"])
            job["entries"].extend(unique_entries)
            job["reasons"].extend(reasons)
            job["skipped"] = len(job["reasons"])
            job["has_more"] = has_more
            job["next_start"] += PLAYLIST_LIMIT
            job.pop("page_error", None)
            if len(job["entries"]) > old_count:
                job["page"] = old_count // PLAYLIST_PAGE_SIZE
        except (DownloadError, ValueError, OSError, sqlite3.Error) as exc:
            LOG.warning("Could not load more playlist tracks: %s", exc)
            job["page_error"] = True
        finally:
            job["loading"] = False
    elif action == "remember":
        if not job["selected"]:
            await query.answer("Сначала отметьте треки, которые уже есть в чате.", show_alert=True)
            return
        selected_entries = [job["entries"][index] for index in sorted(job["selected"])]
        try:
            for url, label, key in selected_entries:
                artist, title = music_metadata({"title": label})
                record_sent_key(job["source_chat_id"], url, key, artist=artist, title=title)
            job["entries"], reasons = filter_sent_tracks_explained(job["source_chat_id"], job["entries"])
        except (sqlite3.Error, OSError) as exc:
            LOG.warning("Could not remember old tracks: %s", exc)
            await query.answer("Не удалось сохранить список. Попробуйте снова.", show_alert=True)
            return
        job["reasons"].extend(reasons)
        job["skipped"] = len(job["reasons"])
        job["selected"] = set()
        job["page"] = 0
        await query.answer(f"Запомнил треков: {len(selected_entries)}.")
        if not job["entries"] and not job["has_more"]:
            jobs.pop(job_id)
            await delete_batch_messages(
                context, job["source_chat_id"], job["source_message_id"], query.message.message_id,
            )
            return
        await query.edit_message_text(playlist_text(job), reply_markup=playlist_markup(job_id, job))
        return
    elif action == "download":
        if not job["selected"]:
            await query.answer("Сначала выберите хотя бы один трек.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            f"Выбрано треков: {len(job['selected'])}. Выберите качество {value.upper()}:",
            reply_markup=quality_markup(job_id, value, mix=True),
        )
        return
    elif action == "quality":
        if value == "back":
            await query.answer()
            await query.edit_message_text(playlist_text(job), reply_markup=playlist_markup(job_id, job))
            return
        try:
            media_type, quality = value.split(":", 1)
        except ValueError:
            await query.answer("Неверное качество.", show_alert=True)
            return
        if media_type not in QUALITY_OPTIONS or quality not in QUALITY_OPTIONS[media_type] or not job["selected"]:
            await query.answer("Выберите треки и качество заново.", show_alert=True)
            return
        jobs.pop(job_id)
        await query.answer()
        selected_entries = [job["entries"][index] for index in sorted(job["selected"])]
        urls = [entry[0] for entry in selected_entries]
        known_keys = {entry[0]: entry[2] for entry in selected_entries}
        queue_delivery(
            update, query, context, urls, media_type, job["source_chat_id"], job["source_message_id"],
            known_keys, job_id=job_id, quality=quality,
        )
        return
    await query.answer()
    await query.edit_message_text(playlist_text(job), reply_markup=playlist_markup(job_id, job))


async def send_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.callback_query.message
    key = (message.chat_id, getattr(message, "message_thread_id", None))
    lock = context.user_data.setdefault("search_panel_locks", {}).setdefault(key, asyncio.Lock())
    async with lock:
        await send_media_impl(update, context)


async def dismiss_stale_media_choice(query, context, job_id: str) -> None:
    if job_id in context.application.bot_data.setdefault("active_batches", {}):
        await query.answer("Загрузка уже идёт.")
        return
    try:
        if load_retry_batch(job_id) is not None:
            await query.answer("Загрузка завершилась с ошибкой. Используйте кнопку повтора.", show_alert=True)
            return
    except (sqlite3.Error, OSError, ValueError) as exc:
        LOG.warning("Could not check stale media choice against retries: %s", exc)
        await query.answer("Не удалось проверить загрузку. Попробуйте позже.", show_alert=True)
        return
    try:
        if query.message.chat.type == "private":
            await context.bot.delete_message(
                chat_id=query.message.chat_id, message_id=query.message.message_id,
            )
    except TelegramError:
        pass
    await query.answer("Эта кнопка устарела. Отправьте запрос снова.", show_alert=True)


async def send_media_impl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        parts = query.data.split(":")
        if len(parts) == 2 and parts[0] in QUALITY_OPTIONS:
            media_type, job_id = parts
            job = context.user_data.setdefault("jobs", {}).get(job_id)
            if job is None:
                await dismiss_stale_media_choice(query, context, job_id)
                return
            if len(job) > 3 and job[3] != query.from_user.id:
                await query.answer("Это чужой выбор.", show_alert=True)
                return
            if job[1] != query.message.chat_id:
                await query.answer("Это выбор из другого чата.", show_alert=True)
                return
            if len(job) > 5 and job[5] != getattr(query.message, "message_thread_id", None):
                await query.answer("Откройте выбор в прежней теме.", show_alert=True)
                return
            refresh_findout_format_timer(context, job_id)
            await query.answer()
            await query.edit_message_text(
                f"Выберите качество {media_type.upper()}:",
                reply_markup=quality_markup(
                    job_id, media_type,
                    dismiss=job_id in context.user_data.setdefault("findout_format_timers", {}),
                ),
            )
            return
        if len(parts) == 3 and parts[0] == "format" and parts[2] in ("back", "close"):
            job_id = parts[1]
            job = context.user_data.setdefault("jobs", {}).get(job_id)
            if job is None:
                await dismiss_stale_media_choice(query, context, job_id)
                return
            if ((len(job) > 3 and job[3] != query.from_user.id) or job[1] != query.message.chat_id
                    or (len(job) > 5 and job[5] != getattr(query.message, "message_thread_id", None))):
                await query.answer("Это чужой выбор.", show_alert=True)
                return
            if parts[2] == "close":
                try:
                    await context.bot.delete_message(chat_id=job[1], message_id=query.message.message_id)
                except TelegramError as exc:
                    LOG.warning("Could not close format selection: %s", exc)
                    await query.answer("Не удалось убрать сообщение. Попробуйте ещё раз.", show_alert=True)
                    return
                context.user_data["jobs"].pop(job_id, None)
                format_timer = context.user_data.setdefault("findout_format_timers", {}).pop(job_id, None)
                if format_timer:
                    format_timer["task"].cancel()
                await query.answer()
                return
            refresh_findout_format_timer(context, job_id)
            await query.answer()
            await query.edit_message_text(
                "Выберите формат для всех ссылок:",
                reply_markup=format_markup(
                    job_id, dismiss=job_id in context.user_data.setdefault("findout_format_timers", {}),
                ),
            )
            return
        if len(parts) != 4 or parts[0] != "quality":
            return
        _, job_id, media_type, quality = parts
    except (AttributeError, ValueError):
        return
    if media_type not in QUALITY_OPTIONS or quality not in QUALITY_OPTIONS[media_type]:
        await query.answer("Неверное качество.", show_alert=True)
        return

    jobs = context.user_data.setdefault("jobs", {})
    job = jobs.get(job_id)
    if job is None:
        await dismiss_stale_media_choice(query, context, job_id)
        return
    if len(job) > 3 and job[3] != query.from_user.id:
        await query.answer("Это чужой выбор.", show_alert=True)
        return
    if job[1] != query.message.chat_id:
        await query.answer("Это выбор из другого чата.", show_alert=True)
        return
    if len(job) > 5 and job[5] != getattr(query.message, "message_thread_id", None):
        await query.answer("Откройте выбор в прежней теме.", show_alert=True)
        return
    format_timer = context.user_data.setdefault("findout_format_timers", {}).get(job_id)
    if format_timer:
        try:
            await query.edit_message_text(
                "Начинаю загрузку…", reply_markup=cancel_markup(job_id),
            )
        except TelegramError as exc:
            LOG.warning("Could not start download from format selection: %s", exc)
            await query.answer("Не удалось начать загрузку. Повторите кнопку.", show_alert=True)
            return
    context.user_data["findout_format_timers"].pop(job_id, None)
    if format_timer:
        format_timer["task"].cancel()
    jobs.pop(job_id)
    await query.answer()
    urls, source_chat_id, source_message_id = job[:3]
    queue_delivery(
        update, query, context, urls, media_type, source_chat_id, source_message_id,
        job[4] if len(job) > 4 else None, job_id=job_id, quality=quality,
    )


def cancel_markup(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✖️ Отменить загрузку", callback_data=f"cancel:{job_id}"),
    ]])


def retry_markup(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Повторить неудачные", callback_data=f"retry:{job_id}:run"),
        InlineKeyboardButton("🗑 Убрать", callback_data=f"retry:{job_id}:close"),
    ]])


async def delete_batch_messages(context, chat_id: int, source_message_id: int, status_message_id: int) -> None:
    for message_id in (source_message_id, status_message_id):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError as exc:
            LOG.warning("Could not delete batch message %s in chat %s: %s", message_id, chat_id, exc)


async def cancel_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    job_id = query.data.split(":", 1)[1]
    state = context.application.bot_data.setdefault("active_batches", {}).get(job_id)
    if not state or state["user_id"] != query.from_user.id:
        await query.answer("Загрузка уже завершена.", show_alert=True)
        return
    state["cancelled"] = True
    await query.answer("Останавливаю после текущего шага…")


async def retry_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, job_id, action = query.data.split(":", 2)
    try:
        job = load_retry_batch(job_id)
    except (sqlite3.Error, OSError, ValueError) as exc:
        LOG.warning("Could not load retry batch: %s", exc)
        await query.answer("Не удалось открыть список ошибок.", show_alert=True)
        return
    if job is None or job["user_id"] != query.from_user.id or job["chat_id"] != query.message.chat_id:
        await query.answer("Эта кнопка больше недоступна.", show_alert=True)
        return
    if action == "close":
        delete_retry_batch(job_id)
        await query.answer()
        await delete_batch_messages(
            context, job["chat_id"], job["source_message_id"], query.message.message_id,
        )
        return
    if job_id in context.application.bot_data.setdefault("active_batches", {}):
        await query.answer("Повтор уже идёт.")
        return
    await query.answer()
    queue_delivery(
        update, query, context, job["urls"], job["media_type"], job["chat_id"],
        job["source_message_id"], job["known_keys"], job_id=job_id, quality=job["quality"],
    )


def queue_delivery(
    update, query, context, urls, media_type, source_chat_id, source_message_id,
    known_keys=None, *, job_id=None, quality=None,
) -> None:
    """Release the update handler while long downloads wait for a worker slot."""
    job_id = job_id or uuid4().hex[:12]
    active = context.application.bot_data.setdefault("active_batches", {})
    state = {"cancelled": False, "user_id": query.from_user.id}
    active[job_id] = state
    work = deliver_media(
        query, context, urls, media_type, source_chat_id, source_message_id,
        known_keys, job_id=job_id, quality=quality, state=state,
    )
    try:
        context.application.create_task(work, update=update)
    except Exception:
        active.pop(job_id, None)
        work.close()
        raise


async def deliver_media(
    query, context, urls, media_type, source_chat_id, source_message_id,
    known_keys=None, *, job_id=None, quality=None, state=None,
) -> None:
    job_id = job_id or uuid4().hex[:12]
    quality = quality or DEFAULT_QUALITY[media_type]
    known_keys = known_keys or {}
    active = context.application.bot_data.setdefault("active_batches", {})
    if state is None:
        state = {"cancelled": False, "user_id": query.from_user.id}
    active[job_id] = state
    success = 0
    failed_urls = []
    failed_numbers = []
    skipped = 0
    try:
        me = await context.bot.get_me()
        use_topics = bool(getattr(me, "has_topics_enabled", False) and query.message.chat.type == "private")
    except TelegramError as exc:
        LOG.warning("Could not check topic mode: %s", exc)
        use_topics = False
    try:
        for index, url in enumerate(urls, start=1):
            if state["cancelled"]:
                break
            await query.edit_message_text(
                f"Скачиваю {media_type.upper()} {quality}{'p' if media_type == 'mp4' else ' кбит/с'}: {index}/{len(urls)}…",
                reply_markup=cancel_markup(job_id),
            )
            try:
                if already_sent(source_chat_id, url, known_keys.get(url, "")):
                    skipped += 1
                    continue
                download_slots = context.application.bot_data.setdefault(
                    "download_slots", asyncio.Semaphore(2)
                )
                async with AsyncExitStack() as resources:
                    await resources.enter_async_context(download_slots)
                    if state["cancelled"]:
                        break
                    temp = resources.enter_context(tempfile.TemporaryDirectory(prefix="telega-"))
                    file_path, artist, title, cover = await asyncio.to_thread(
                        download_media, url, media_type, Path(temp), quality
                    )
                    if state["cancelled"]:
                        break
                    lock = context.application.bot_data.setdefault(
                        f"delivery_lock:{source_chat_id}", asyncio.Lock()
                    )
                    async with lock:
                        if state["cancelled"]:
                            break
                        if already_sent(source_chat_id, url, song_key(artist, title)):
                            skipped += 1
                            continue
                        filename = display_filename(artist, title, media_type)
                        thread = None
                        misc_thread = None
                        if use_topics:
                            group_artist = assigned_artist(source_chat_id, artist or "Без исполнителя", title)
                            count = artist_track_count(source_chat_id, group_artist)
                            if count + 1 >= ARTIST_TOPIC_THRESHOLD:
                                thread = await artist_topic(context, source_chat_id, group_artist)
                            else:
                                thread = await artist_topic(context, source_chat_id, None, misc=True)
                        target = {"message_thread_id": thread} if thread is not None else {}
                        with ExitStack() as stack:
                            media = stack.enter_context(file_path.open("rb"))
                            if media_type == "mp3":
                                cover_file = stack.enter_context(cover.open("rb")) if cover else None
                                sent_message = await context.bot.send_audio(
                                    chat_id=query.message.chat_id, audio=media, performer=artist,
                                    title=title, filename=filename, thumbnail=cover_file,
                                    write_timeout=120, **target,
                                )
                            else:
                                sent_message = await context.bot.send_video(
                                    chat_id=query.message.chat_id, video=media,
                                    caption=f"{artist} — {title}" if artist else title,
                                    filename=filename, supports_streaming=True,
                                    write_timeout=120, **target,
                                )
                        try:
                            sent_file = getattr(sent_message, "audio" if media_type == "mp3" else "video", None)
                            record_sent_track(
                                source_chat_id, url, artist, title,
                                file_id=getattr(sent_file, "file_id", None), media_type=media_type,
                                message_id=sent_message.message_id, message_thread_id=thread,
                            )
                            if use_topics and count + 1 == ARTIST_TOPIC_THRESHOLD:
                                with closing(history_connection()) as connection:
                                    row = connection.execute(
                                        "SELECT message_thread_id FROM artist_topics "
                                        "WHERE chat_id = ? AND artist_key = '__misc__'", (source_chat_id,),
                                    ).fetchone()
                                if row:
                                    misc_thread = row[0]
                            if misc_thread is not None:
                                await move_recent_misc_tracks(
                                    context, source_chat_id, group_artist, misc_thread, thread,
                                )
                        except (sqlite3.Error, OSError) as exc:
                            LOG.warning("Could not record sent track: %s", exc)
                success += 1
            except (DownloadError, ValueError, TelegramError, OSError, sqlite3.Error) as exc:
                LOG.warning("Could not process video %s: %s", url, exc)
                failed_urls.append(url)
                failed_numbers.append(index)

        if state["cancelled"]:
            delete_retry_batch(job_id)
            await delete_batch_messages(
                context, source_chat_id, source_message_id, query.message.message_id,
            )
            return
        if failed_urls:
            save_retry_batch(
                job_id, query.from_user.id, source_chat_id, source_message_id,
                media_type, failed_urls,
                {url: known_keys[url] for url in failed_urls if url in known_keys},
                quality=quality,
            )
            await query.edit_message_text(
                f"Отправлено: {success}, уже были в чате: {skipped}. Не получилось: "
                f"{', '.join(map(str, failed_numbers))}. Повторите только эти ссылки.",
                reply_markup=retry_markup(job_id),
            )
            return
        delete_retry_batch(job_id)
        await delete_batch_messages(
            context, source_chat_id, source_message_id, query.message.message_id,
        )
    finally:
        active.pop(job_id, None)


def library_text(session: dict) -> str:
    artist_index = session["artist_index"]
    if artist_index is None:
        return (
            f"📚 Исполнители: {len(session['groups'])}. Выберите исполнителя, чтобы увидеть названия песен. "
            "Новые аудио сначала попадают в NoName; после 5 песен у исполнителя появляется своя тема."
        )
    artist, tracks = session["groups"][artist_index]
    page = session["song_page"]
    start = page * PLAYLIST_PAGE_SIZE
    songs = [f"{index + 1}. {track['title']}" for index, track in enumerate(tracks[start:start + PLAYLIST_PAGE_SIZE], start)]
    return f"🎤 {artist}\nСохранено треков: {len(tracks)}\n\n" + "\n".join(songs)


def library_markup(job_id: str, session: dict) -> InlineKeyboardMarkup:
    artist_index = session["artist_index"]
    if artist_index is None:
        items = session["groups"]
        page = session["artist_page"]
        page_action = "ap"
    else:
        items = session["groups"][artist_index][1]
        page = session["song_page"]
        page_action = "sp"
    start = page * PLAYLIST_PAGE_SIZE
    rows = []
    if artist_index is None:
        for index in range(start, min(start + PLAYLIST_PAGE_SIZE, len(items))):
            artist, tracks = items[index]
            label = f"{artist} ({len(tracks)})".replace("\n", " ")
            if len(label) > 48:
                label = label[:47] + "…"
            rows.append([InlineKeyboardButton(
                f"🎤 {label}", callback_data=f"lib:{job_id}:artist:{index}",
            )])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("◀️", callback_data=f"lib:{job_id}:{page_action}:{page - 1}"))
    navigation.append(InlineKeyboardButton(
        f"{page + 1}/{(len(items) - 1) // PLAYLIST_PAGE_SIZE + 1}",
        callback_data=f"lib:{job_id}:{page_action}:{page}",
    ))
    if start + PLAYLIST_PAGE_SIZE < len(items):
        navigation.append(InlineKeyboardButton("▶️", callback_data=f"lib:{job_id}:{page_action}:{page + 1}"))
    rows.append(navigation)
    if artist_index is not None:
        rows.append([InlineKeyboardButton("◀️ К исполнителям", callback_data=f"lib:{job_id}:back:0")])
    elif session.get("zip_enabled"):
        rows.append([InlineKeyboardButton("📦 Скачать все MP3 ZIP", callback_data="zip:own")])
    return InlineKeyboardMarkup(rows)


async def artists_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        groups = topic_library_groups(update.effective_chat.id)
    except (sqlite3.Error, OSError) as exc:
        LOG.warning("Could not read library: %s", exc)
        await update.effective_message.reply_text("Не удалось открыть список исполнителей.")
        return
    if not groups:
        await update.effective_message.reply_text("Пока нет сохранённых песен. Скачайте MP3 или MP4 через бота.")
        return
    sessions = context.user_data.setdefault("libraries", {})
    if len(sessions) >= 10:
        sessions.pop(next(iter(sessions)))
    job_id = uuid4().hex[:12]
    session = {
        "groups": groups, "artist_index": None, "artist_page": 0, "song_page": 0,
        "zip_enabled": update.effective_chat.type == "private",
    }
    sessions[job_id] = session
    await update.effective_message.reply_text(
        library_text(session), reply_markup=library_markup(job_id, session)
    )


async def clear_saved_search_ui(context, key: tuple[int, int | None]) -> None:
    state = context.user_data.setdefault("saved_search_ui", {}).pop(key, None)
    if not state:
        return
    if state.get("timer"):
        state["timer"].cancel()
    try:
        await context.bot.delete_message(chat_id=key[0], message_id=state["message_id"])
    except TelegramError:
        pass


async def clear_findout_ui(context, key: tuple[int, int | None]) -> None:
    empty = context.user_data.setdefault("findout_empty_ui", {}).pop(key, None)
    if empty:
        empty["task"].cancel()
        try:
            await context.bot.delete_message(chat_id=key[0], message_id=empty["message_id"])
        except TelegramError:
            pass
    old_job_id = context.user_data.setdefault("findout_current", {}).pop(key, None)
    old_job = context.user_data.setdefault("youtube_searches", {}).pop(old_job_id, None) if old_job_id else None
    if old_job:
        if old_job.get("timer"):
            old_job["timer"].cancel()
        try:
            await context.bot.delete_message(chat_id=key[0], message_id=old_job["status_message_id"])
        except TelegramError:
            pass
    for format_id, record in list(context.user_data.setdefault("findout_format_timers", {}).items()):
        if (record["chat_id"], record["thread_id"]) == key:
            record["task"].cancel()
            context.user_data["findout_format_timers"].pop(format_id, None)
            context.user_data.setdefault("jobs", {}).pop(format_id, None)
            try:
                await context.bot.delete_message(chat_id=key[0], message_id=record["message_id"])
            except TelegramError:
                pass


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    key = (update.effective_chat.id, getattr(message, "message_thread_id", None))
    lock = context.user_data.setdefault("search_panel_locks", {}).setdefault(key, asyncio.Lock())
    async with lock:
        await search_command_impl(update, context)


async def search_command_impl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = update.effective_chat.id
    thread_id = getattr(message, "message_thread_id", None)
    key = (chat_id, thread_id)
    await clear_findout_ui(context, key)
    query_text = " ".join(context.args).strip()
    matches = []
    if query_text:
        terms = re.findall(r"\w+", unicodedata.normalize("NFKC", query_text).casefold())
        try:
            groups = library_groups(chat_id)
        except (sqlite3.Error, OSError) as exc:
            LOG.warning("Could not search library: %s", exc)
            groups = []
        for artist, tracks in groups:
            topic = artist if len(tracks) >= ARTIST_TOPIC_THRESHOLD else MISC_TOPIC_NAME
            for track in tracks:
                label = f"{track['artist']} — {track['title']}"
                words = re.findall(r"\w+", unicodedata.normalize("NFKC", f"{artist} {label}").casefold())
                scores = []
                for term in terms:
                    if any(term == word or (
                        len(term) >= 5 and word.startswith(term) and len(word) - len(term) == 1
                    ) for word in words):
                        scores.append(0)
                    elif len(term) >= 4 and any(
                        difflib.SequenceMatcher(None, term, word).ratio() >= 0.86 for word in words
                    ):
                        scores.append(1)
                    else:
                        break
                else:
                    if scores:
                        matches.append((sum(scores), label, topic, track))
        matches.sort(key=lambda item: (item[0], not bool(item[3]["audio_file_id"]), item[1].casefold()))
    job_id = uuid4().hex[:12]
    if not query_text:
        text = "Напишите /search и имя исполнителя или название песни."
    elif matches:
        lines = [f"🔎 Найдено: {len(matches)}. MP3 можно отправить кнопкой ниже:"]
        for _, label, topic, track in matches[:15]:
            suffix = " (MP4)" if not track["audio_file_id"] and track.get("video_file_id") else " (без MP3)" if not track["audio_file_id"] else ""
            lines.append(f"• {label}{suffix} → {topic}")
        if len(matches) > 15:
            lines.append("Показаны первые 15. Уточните запрос.")
        text = "\n".join(lines)
    else:
        text = "По этому запросу сохранённых песен не найдено."
    rows = [
        [InlineKeyboardButton(f"🎵 {label}"[:64], callback_data=f"searchsong:{job_id}:{index}")]
        for index, (_, label, _, track) in enumerate(matches[:15]) if track["audio_file_id"]
    ]
    rows.append([InlineKeyboardButton(
        "✖️ Закрыть", callback_data=f"searchclose:{update.effective_user.id}:{job_id}",
    )])
    markup = InlineKeyboardMarkup(rows)
    states = context.user_data.setdefault("saved_search_ui", {})
    old = states.get(key)
    status = None
    if old:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=old["message_id"], text=text[:4000], reply_markup=markup,
            )
            status_id = old["message_id"]
        except TelegramError:
            status = await message.reply_text(text[:4000], reply_markup=markup)
            status_id = status.message_id
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=old["message_id"])
            except TelegramError:
                pass
    else:
        status = await message.reply_text(text[:4000], reply_markup=markup)
        status_id = status.message_id
    if old and old.get("timer"):
        old["timer"].cancel()
    state = {
        "job_id": job_id, "user_id": update.effective_user.id, "chat_id": chat_id,
        "message_id": status_id, "matches": matches[:15], "timer": None,
        "text": text[:4000], "markup": markup,
    }
    states[key] = state
    state["timer"] = asyncio.create_task(expire_saved_search(context, key, job_id))
    try:
        await context.bot.delete_message(
            chat_id=chat_id, message_id=message.message_id,
        )
    except TelegramError:
        pass


async def expire_saved_search(context, key: tuple[int, int | None], job_id: str) -> None:
    try:
        await asyncio.sleep(SEARCH_UI_TIMEOUT)
    except asyncio.CancelledError:
        return
    states = context.user_data.get("saved_search_ui", {})
    state = states.get(key)
    if not state or state["job_id"] != job_id:
        return
    states.pop(key, None)
    try:
        await context.bot.delete_message(chat_id=key[0], message_id=state["message_id"])
    except TelegramError:
        pass


async def search_song_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, job_id, raw_index = query.data.split(":", 2)
    key = (query.message.chat_id, getattr(query.message, "message_thread_id", None))
    state = context.user_data.setdefault("saved_search_ui", {}).get(key)
    if not state or state["job_id"] != job_id or state["message_id"] != query.message.message_id:
        await query.answer("Поиск устарел. Отправьте /search ещё раз.", show_alert=True)
        return
    if state["user_id"] != query.from_user.id:
        await query.answer("Это чужой поиск.", show_alert=True)
        return
    index = int(raw_index)
    if index >= len(state["matches"]):
        await query.answer("Песня недоступна.", show_alert=True)
        return
    track = state["matches"][index][3]
    if not track["audio_file_id"]:
        await query.answer("Для этой записи нет сохранённого MP3.", show_alert=True)
        return
    if state.get("sending"):
        await query.answer("Песня уже отправляется.")
        return
    state["sending"] = True
    try:
        await query.answer("Отправляю MP3…")
    except TelegramError as exc:
        LOG.warning("Could not acknowledge saved search selection: %s", exc)
        state["sending"] = False
        return
    target = {"message_thread_id": key[1]} if key[1] is not None else {}
    try:
        await context.bot.send_audio(
            chat_id=key[0], audio=track["audio_file_id"],
            performer=track["artist"], title=track["title"], **target,
        )
    except TelegramError as exc:
        LOG.warning("Could not send saved search MP3: %s", exc)
        state["sending"] = False
        if context.user_data["saved_search_ui"].get(key) is not state:
            return
        try:
            await query.edit_message_text(
                state["text"] + "\n\n⚠️ Не удалось отправить MP3. Нажмите песню ещё раз.",
                reply_markup=state["markup"],
            )
        except TelegramError:
            try:
                await context.bot.send_message(
                    chat_id=key[0], text="Не удалось отправить MP3. Нажмите песню в поиске ещё раз.",
                    **target,
                )
            except TelegramError as notify_exc:
                LOG.warning("Could not report saved MP3 failure: %s", notify_exc)
        return
    if state["timer"]:
        state["timer"].cancel()
    if context.user_data["saved_search_ui"].get(key) is state:
        context.user_data["saved_search_ui"].pop(key, None)
        try:
            await context.bot.delete_message(chat_id=key[0], message_id=state["message_id"])
        except TelegramError:
            pass


async def findout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    key = (message.chat_id, getattr(message, "message_thread_id", None))
    lock = context.user_data.setdefault("search_panel_locks", {}).setdefault(key, asyncio.Lock())
    async with lock:
        await findout_command_impl(update, context)


async def findout_command_impl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    search_text = " ".join(context.args).strip()
    key = (message.chat_id, getattr(message, "message_thread_id", None))
    await clear_saved_search_ui(context, key)
    await clear_findout_ui(context, key)
    current = context.user_data.setdefault("findout_current", {})
    if len(search_text) < 2 or len(search_text) > 150:
        status = await message.reply_text(
            "Напишите /findout и название песни, исполнителя или оба. Например: /findout Jace June Come Home",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✖️ Закрыть", callback_data=f"searchclose:{update.effective_user.id}"),
            ]]),
        )
        try:
            await message.delete()
        except TelegramError:
            pass
        schedule_findout_empty(context, key, status.message_id)
        return
    status = await message.reply_text("Ищу на YouTube…")
    try:
        await context.bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
    except TelegramError:
        pass
    search_failed = False
    try:
        results = await asyncio.to_thread(search_youtube, search_text)
        results = results[:SEARCH_RESULT_LIMIT]
    except (DownloadError, ValueError, OSError) as exc:
        LOG.warning("Could not search YouTube: %s", exc)
        results, search_failed = [], True
    if not results:
        await status.edit_text(
            ("Не удалось выполнить поиск YouTube. Попробуйте ещё раз." if search_failed else
             "По этому запросу ничего не нашлось. Попробуйте указать исполнителя и название вместе."),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✖️ Закрыть", callback_data=f"searchclose:{update.effective_user.id}"),
            ]]),
        )
        schedule_findout_empty(context, key, status.message_id)
        return
    try:
        video_ids, song_keys = sent_tracks(message.chat_id)
    except (sqlite3.Error, OSError) as exc:
        LOG.warning("Could not check found tracks against history: %s", exc)
        video_ids, song_keys = set(), set()
    searches = context.user_data.setdefault("youtube_searches", {})
    if len(searches) >= 10:
        searches.pop(next(iter(searches)))
    job_id = uuid4().hex[:12]
    searches[job_id] = {
        "entries": results,
        "user_id": update.effective_user.id,
        "chat_id": message.chat_id,
        "source_message_id": message.message_id,
        "source_deleted": True,
        "status_message_id": status.message_id,
        "selected": set(),
        "timer": None,
        "thread_id": key[1],
    }
    current[key] = job_id
    try:
        await status.edit_text(
            f"🔎 YouTube: {search_text}\nОтметьте до 10 записей. ✅ — уже есть в чате.",
            reply_markup=findout_markup(job_id, searches[job_id], video_ids, song_keys),
        )
    except TelegramError as exc:
        LOG.warning("Could not show YouTube search results: %s", exc)
        searches.pop(job_id, None)
        current.pop(key, None)
        try:
            await context.bot.delete_message(chat_id=message.chat_id, message_id=status.message_id)
        except TelegramError:
            pass
        try:
            error = await message.reply_text("Не удалось показать результаты. Повторите /findout.")
        except TelegramError:
            return
        schedule_findout_empty(context, key, error.message_id)
        return
    searches[job_id]["timer"] = asyncio.create_task(expire_findout(context, job_id))


def findout_markup(job_id: str, job: dict, video_ids: set, song_keys: set) -> InlineKeyboardMarkup:
    rows = []
    selected = job.setdefault("selected", set())
    for index, (url, label, key, duration) in enumerate(job["entries"]):
        video_id = parse_qs(urlsplit(url).query)["v"][0]
        saved = video_id in video_ids or bool(key and key in song_keys)
        mark = "✅" if saved else "☑️" if index in selected else "🎵"
        time_label = f" [{duration // 60}:{duration % 60:02d}]" if duration is not None else ""
        rows.append([InlineKeyboardButton(
            f"{mark} {index + 1}. {label[:42]}{time_label}", callback_data=f"find:{job_id}:{index}",
        )])
    rows.append([InlineKeyboardButton(
        f"⬇️ Скачать выбранные ({len(selected)})", callback_data=f"find:{job_id}:download",
    )])
    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data=f"find:{job_id}:close")])
    return InlineKeyboardMarkup(rows)


async def expire_findout(context, job_id: str) -> None:
    try:
        await asyncio.sleep(SEARCH_UI_TIMEOUT)
    except asyncio.CancelledError:
        return
    searches = context.user_data.get("youtube_searches", {})
    job = searches.get(job_id)
    if not job:
        return
    key = (job["chat_id"], job.get("thread_id"))
    lock = context.user_data.setdefault("search_panel_locks", {}).setdefault(key, asyncio.Lock())
    async with lock:
        job = searches.get(job_id)
        if not job or job.get("timer") is not asyncio.current_task():
            return
        searches.pop(job_id, None)
        if context.user_data.setdefault("findout_current", {}).get(key) == job_id:
            context.user_data["findout_current"].pop(key, None)
        try:
            await context.bot.delete_message(chat_id=job["chat_id"], message_id=job["status_message_id"])
        except TelegramError:
            pass


def schedule_findout_empty(context, key: tuple[int, int | None], message_id: int) -> None:
    context.user_data.setdefault("findout_empty_ui", {})[key] = {
        "message_id": message_id,
        "task": asyncio.create_task(expire_findout_empty(context, key, message_id)),
    }


async def expire_findout_empty(context, key: tuple[int, int | None], message_id: int) -> None:
    try:
        await asyncio.sleep(SEARCH_UI_TIMEOUT)
    except asyncio.CancelledError:
        return
    empty = context.user_data.setdefault("findout_empty_ui", {})
    if empty.get(key, {}).get("message_id") != message_id:
        return
    empty.pop(key, None)
    try:
        await context.bot.delete_message(chat_id=key[0], message_id=message_id)
    except TelegramError:
        pass


def refresh_findout_format_timer(context, job_id: str) -> None:
    record = context.user_data.setdefault("findout_format_timers", {}).get(job_id)
    if record:
        record["task"].cancel()
        record["task"] = asyncio.create_task(expire_findout_format(context, job_id))


async def expire_findout_format(context, job_id: str) -> None:
    try:
        await asyncio.sleep(SEARCH_UI_TIMEOUT)
    except asyncio.CancelledError:
        return
    record = context.user_data.setdefault("findout_format_timers", {}).get(job_id)
    if not record:
        return
    key = (record["chat_id"], record["thread_id"])
    lock = context.user_data.setdefault("search_panel_locks", {}).setdefault(key, asyncio.Lock())
    async with lock:
        timers = context.user_data.setdefault("findout_format_timers", {})
        record = timers.get(job_id)
        if not record or record["task"] is not asyncio.current_task():
            return
        timers.pop(job_id, None)
        context.user_data.setdefault("jobs", {}).pop(job_id, None)
        try:
            await context.bot.delete_message(chat_id=record["chat_id"], message_id=record["message_id"])
        except TelegramError:
            pass


async def findout_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.callback_query.message
    key = (message.chat_id, getattr(message, "message_thread_id", None))
    lock = context.user_data.setdefault("search_panel_locks", {}).setdefault(key, asyncio.Lock())
    async with lock:
        await findout_action_impl(update, context)


async def findout_action_impl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, job_id, choice = query.data.split(":", 2)
    searches = context.user_data.setdefault("youtube_searches", {})
    job = searches.get(job_id)
    if job is None or job["chat_id"] != query.message.chat_id:
        await query.answer("Поиск устарел. Отправьте /findout ещё раз.", show_alert=True)
        return
    if job["user_id"] != query.from_user.id:
        await query.answer("Это чужой поиск.", show_alert=True)
        return
    if job.get("thread_id") != getattr(query.message, "message_thread_id", None):
        await query.answer("Откройте поиск в прежней теме.", show_alert=True)
        return
    if choice == "close":
        searches.pop(job_id, None)
        if job.get("timer"):
            job["timer"].cancel()
        context.user_data.setdefault("findout_current", {}).pop((job["chat_id"], job.get("thread_id")), None)
        await query.answer()
        if job.get("source_deleted"):
            try:
                await context.bot.delete_message(chat_id=job["chat_id"], message_id=query.message.message_id)
            except TelegramError:
                pass
        else:
            await delete_batch_messages(context, job["chat_id"], job["source_message_id"], query.message.message_id)
        return
    if choice == "download":
        selected = sorted(job.setdefault("selected", set()))
        if not selected:
            await query.answer("Сначала отметьте хотя бы одну запись.", show_alert=True)
            return
        available = []
        for index in selected:
            url, label, key, _ = job["entries"][index]
            if not already_sent(job["chat_id"], url, key):
                available.append((url, label, key))
        skipped_count = len(selected) - len(available)
        if skipped_count:
            job["selected"] = {index for index in selected if any(job["entries"][index][0] == item[0] for item in available)}
        if not available:
            await query.answer("Выбранные песни уже есть в чате.", show_alert=True)
            return
        urls = [item[0] for item in available]
        known_keys = {item[0]: item[2] for item in available}
        try:
            await query.edit_message_text(
                f"Выбрано записей: {len(urls)}."
                + (f" Уже в чате, пропущено: {skipped_count}." if skipped_count else "")
                + " Теперь выберите формат:",
                reply_markup=format_markup(job_id, dismiss=True),
            )
        except TelegramError as exc:
            LOG.warning("Could not open format selection: %s", exc)
            await query.answer("Не удалось открыть выбор формата. Повторите кнопку.", show_alert=True)
            return
        jobs = context.user_data.setdefault("jobs", {})
        if len(jobs) >= 10:
            jobs.pop(next(iter(jobs)))
        jobs[job_id] = (
            urls, job["chat_id"], job["source_message_id"], job["user_id"],
            known_keys, job.get("thread_id"),
        )
        searches.pop(job_id, None)
        if job.get("timer"):
            job["timer"].cancel()
        context.user_data.setdefault("findout_current", {}).pop((job["chat_id"], job.get("thread_id")), None)
        context.user_data.setdefault("findout_format_timers", {})[job_id] = {
            "chat_id": job["chat_id"], "thread_id": job.get("thread_id"),
            "message_id": query.message.message_id,
            "task": asyncio.create_task(expire_findout_format(context, job_id)),
        }
        await query.answer()
        return
    index = int(choice)
    if index >= len(job["entries"]):
        await query.answer("Выберите запись заново.", show_alert=True)
        return
    url, label, key, _ = job["entries"][index]
    if already_sent(job["chat_id"], url, key):
        await query.answer("Эта песня уже есть в чате.", show_alert=True)
        return
    selected = job.setdefault("selected", set())
    if index in selected:
        selected.remove(index)
    else:
        selected.add(index)
    try:
        video_ids, song_keys = sent_tracks(job["chat_id"])
    except (sqlite3.Error, OSError):
        video_ids, song_keys = set(), set()
    if job.get("timer"):
        job["timer"].cancel()
    job["timer"] = asyncio.create_task(expire_findout(context, job_id))
    await query.answer()
    await query.edit_message_text(
        f"🔎 YouTube: выбрано {len(selected)} записей. ✅ — уже есть в чате.",
        reply_markup=findout_markup(job_id, job, video_ids, song_keys),
    )


def stats_view(chat_id: int, user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    groups = library_groups(chat_id)
    groups.sort(key=lambda group: (-len(group[1]), group[0].casefold()))
    total = sum(len(tracks) for _, tracks in groups)
    misc = sum(len(tracks) for _, tracks in groups if len(tracks) < ARTIST_TOPIC_THRESHOLD)
    dedicated = sum(len(tracks) >= ARTIST_TOPIC_THRESHOLD for _, tracks in groups)
    page_size = 20
    page_count = max(1, (len(groups) - 1) // page_size + 1)
    page = min(page, page_count - 1)
    lines = [
        f"📊 Песен: {total} · Исполнителей: {len(groups)}",
        f"Отдельных тем: {dedicated} · В NoName: {misc}",
        "",
    ]
    if not groups:
        lines.append("Пока нет сохранённых песен.")
    for artist, tracks in groups[page * page_size:(page + 1) * page_size]:
        count = len(tracks)
        suffix = f" · до своей темы {ARTIST_TOPIC_THRESHOLD - count}" if count < ARTIST_TOPIC_THRESHOLD else ""
        lines.append(f"{artist}: {count}{suffix}")
    if page_count > 1:
        lines.append(f"\nСтраница {page + 1}/{page_count}")
    rows = []
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("◀️", callback_data=f"stats:{user_id}:{page - 1}"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("▶️", callback_data=f"stats:{user_id}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data=f"stats:{user_id}:close")])
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(rows)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        text, markup = stats_view(update.effective_chat.id, update.effective_user.id, 0)
    except (sqlite3.Error, OSError) as exc:
        LOG.warning("Could not open stats: %s", exc)
        await update.effective_message.reply_text("Не удалось открыть статистику.")
        return
    await update.effective_message.reply_text(text, reply_markup=markup)
    try:
        await update.effective_message.delete()
    except TelegramError:
        pass


async def stats_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, owner, action = query.data.split(":", 2)
    if query.from_user.id != int(owner):
        await query.answer("Это чужая статистика.", show_alert=True)
        return
    await query.answer()
    if action == "close":
        try:
            await query.message.delete()
        except TelegramError:
            pass
        return
    try:
        text, markup = stats_view(query.message.chat_id, int(owner), int(action))
        await query.edit_message_text(text, reply_markup=markup)
    except (sqlite3.Error, OSError) as exc:
        LOG.warning("Could not update stats: %s", exc)


async def change_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    reply = message.reply_to_message
    if reply is None or not (reply.audio or reply.video):
        await message.reply_text("Ответьте командой /favorite на сохранённую песню. Для удаления — /unfavorite.")
        return
    enabled = message.text.split()[0].split("@", 1)[0] == "/favorite"
    try:
        if reply.audio:
            found = await sync_favorite_copy(context.application, message.chat_id, reply.message_id, enabled)
        else:
            found = set_favorite(message.chat_id, reply.message_id, enabled)
    except (sqlite3.Error, OSError, TelegramError) as exc:
        LOG.warning("Could not update favorite: %s", exc)
        await message.reply_text("Не удалось обновить избранное.")
        return
    if not found:
        await message.reply_text("Этой песни нет в каталоге бота.")
        return
    try:
        await context.bot.set_message_reaction(
            chat_id=message.chat_id, message_id=reply.message_id,
            reaction="❤️" if enabled else [],
        )
    except TelegramError as exc:
        LOG.info("Could not mark favorite with reaction: %s", exc)
    try:
        await message.delete()
    except TelegramError:
        pass


def favorites_view(chat_id: int, user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    tracks = favorite_tracks(chat_id)
    page_size = 20
    page_count = max(1, (len(tracks) - 1) // page_size + 1)
    page = min(page, page_count - 1)
    lines = [f"❤️ Избранное: {len(tracks)}"]
    if tracks:
        for index, (artist, title) in enumerate(tracks[page * page_size:(page + 1) * page_size], page * page_size + 1):
            lines.append(f"{index}. {artist} — {title}")
        if page_count > 1:
            lines.append(f"Страница {page + 1}/{page_count}")
    else:
        lines.append("Ответьте /favorite на песню, чтобы добавить её сюда.")
    rows = []
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("◀️", callback_data=f"fav:{user_id}:{page - 1}"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("▶️", callback_data=f"fav:{user_id}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data=f"fav:{user_id}:close")])
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(rows)


async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        text, markup = favorites_view(update.effective_chat.id, update.effective_user.id, 0)
    except (sqlite3.Error, OSError) as exc:
        LOG.warning("Could not open favorites: %s", exc)
        await update.effective_message.reply_text("Не удалось открыть избранное.")
        return
    await update.effective_message.reply_text(text, reply_markup=markup)
    try:
        await update.effective_message.delete()
    except TelegramError:
        pass


async def favorites_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, owner, action = query.data.split(":", 2)
    if query.from_user.id != int(owner):
        await query.answer("Это чужой список.", show_alert=True)
        return
    await query.answer()
    if action == "close":
        try:
            await query.message.delete()
        except TelegramError:
            pass
        return
    try:
        text, markup = favorites_view(query.message.chat_id, int(owner), int(action))
        await query.edit_message_text(text, reply_markup=markup)
    except (sqlite3.Error, OSError) as exc:
        LOG.warning("Could not update favorites: %s", exc)


async def close_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    user_id = int(parts[1])
    if query.from_user.id != user_id:
        await query.answer("Это чужой поиск.", show_alert=True)
        return
    key = (query.message.chat_id, getattr(query.message, "message_thread_id", None))
    state = context.user_data.setdefault("saved_search_ui", {}).get(key)
    if len(parts) == 3 and (not state or state["job_id"] != parts[2]):
        await query.answer("Этот поиск уже заменён.", show_alert=True)
        return
    if state and state["message_id"] == query.message.message_id:
        context.user_data["saved_search_ui"].pop(key, None)
        if state["timer"]:
            state["timer"].cancel()
    empty = context.user_data.setdefault("findout_empty_ui", {}).get(key)
    if empty and empty["message_id"] == query.message.message_id:
        context.user_data["findout_empty_ui"].pop(key, None)
        empty["task"].cancel()
    await query.answer()
    try:
        await query.message.delete()
    except TelegramError:
        pass


async def library_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, job_id, action, raw_index = query.data.split(":", 3)
    session = context.user_data.setdefault("libraries", {}).get(job_id)
    if session is None:
        try:
            groups = topic_library_groups(query.message.chat_id)
        except (sqlite3.Error, OSError) as exc:
            LOG.warning("Could not restore artist menu: %s", exc)
            await query.answer("Не удалось открыть список исполнителей.", show_alert=True)
            return
        if not groups:
            await query.answer("Список исполнителей пуст.", show_alert=True)
            return
        session = {
            "groups": groups, "artist_index": None, "artist_page": 0, "song_page": 0,
            "zip_enabled": query.message.chat.type == "private",
        }
        if action == "sp":
            current_artist = (query.message.text or "").split("\n", 1)[0].removeprefix("🎤 ")
            session["artist_index"] = next(
                (position for position, (artist, _) in enumerate(groups) if artist == current_artist), None
            )
            if session["artist_index"] is None:
                await query.answer("Откройте /artists ещё раз.", show_alert=True)
                return
        context.user_data["libraries"][job_id] = session
        LOG.info("Restored artist menu for chat %s", query.message.chat_id)
    index = int(raw_index)
    if action == "artist":
        if index >= len(session["groups"]):
            await query.answer()
            return
        session["artist_index"] = index
        session["song_page"] = 0
    elif action == "back":
        session["artist_index"] = None
    elif action in {"ap", "sp"}:
        if action == "sp" and session["artist_index"] is None:
            await query.answer()
            return
        items = session["groups"] if action == "ap" else session["groups"][session["artist_index"]][1]
        field = "artist_page" if action == "ap" else "song_page"
        if index * PLAYLIST_PAGE_SIZE >= len(items) or index == session[field]:
            await query.answer()
            return
        session[field] = index
    elif action == "song":
        await query.answer("Песня уже в чате. Повторно отправлять её не буду.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(library_text(session), reply_markup=library_markup(job_id, session))


async def configure_commands(app: Application) -> None:
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Как пользоваться ботом"),
            BotCommand("artists", "Музыка по исполнителям"),
            BotCommand("renametopic", "Переименовать текущую тему"),
            BotCommand("zip", "Скачать свои MP3 ZIP частями"),
            BotCommand("search", "Найти песню или исполнителя"),
            BotCommand("findout", "Найти песню на YouTube"),
            BotCommand("favorite", "Добавить песню в избранное (ответом)"),
            BotCommand("unfavorite", "Удалить песню из избранного (ответом)"),
            BotCommand("favorites", "Мои избранные песни"),
            BotCommand("stats", "Статистика песен и исполнителей"),
            BotCommand("share", "Поделиться подборкой MP3"),
            BotCommand("unshare", "Закрыть доступ к подборке"),
        ])
    except TelegramError as exc:
        LOG.warning("Could not update bot commands: %s", exc)
    resume_transient_cleanup(app.bot)
    asyncio.create_task(resume_pending_shared_syncs(app))
    await start_reaction_listener(app)
    app.bot_data["favorite_reconcile_task"] = asyncio.create_task(reconcile_favorite_topic(app))


def has_heart(reactions: list) -> bool:
    return any(
        isinstance(reaction, types.ReactionEmoji) and reaction.emoticon.rstrip("\ufe0f") == "❤"
        for reaction in reactions
    )


async def handle_reaction_event(app: Application, event: types.UpdateBotMessageReaction) -> None:
    if not isinstance(event, types.UpdateBotMessageReaction):
        return
    if not isinstance(event.peer, types.PeerUser) or not isinstance(event.actor, types.PeerUser):
        return
    chat_id = event.peer.user_id
    if event.actor.user_id != chat_id:
        return
    was_favorite = has_heart(event.old_reactions)
    is_favorite = has_heart(event.new_reactions)
    if was_favorite == is_favorite:
        return
    await sync_favorite_copy(app, chat_id, event.msg_id, is_favorite)


async def start_reaction_listener(app: Application) -> None:
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        LOG.info("Telegram API credentials not set; reaction favorites are disabled")
        return
    session_path = os.environ.get("TELEGA_BOT_SESSION_PATH") or str(HISTORY_DB.parent / "bot_reactions")
    try:
        client = TelegramClient(session_path, int(api_id), api_hash, catch_up=True)
    except (ValueError, OSError) as exc:
        LOG.warning("Invalid Telegram API configuration for reaction listener: %s", exc)
        return

    async def on_raw(event):
        try:
            await handle_reaction_event(app, event)
        except (sqlite3.Error, OSError, TelegramError) as exc:
            LOG.warning("Could not sync favorite reaction: %s", exc)

    client.add_event_handler(on_raw, events.Raw)
    app.bot_data["reaction_client"] = client
    try:
        await client.start(bot_token=os.environ["TELEGRAM_BOT_TOKEN"])
    except Exception as exc:
        LOG.warning("Could not start reaction listener: %s", exc)
        app.bot_data.pop("reaction_client", None)
        await client.disconnect()
        return
    LOG.info("Telegram reaction listener started")


async def stop_reaction_listener(app: Application) -> None:
    reconcile_task = app.bot_data.pop("favorite_reconcile_task", None)
    if reconcile_task is not None:
        reconcile_task.cancel()
        try:
            await reconcile_task
        except asyncio.CancelledError:
            pass
    client = app.bot_data.pop("reaction_client", None)
    if client is not None:
        await client.disconnect()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    load_dotenv(Path(__file__).with_name(".env"))
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Укажите TELEGRAM_BOT_TOKEN в переменных окружения.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("Установите ffmpeg и ffprobe и добавьте их в PATH.")
    with closing(history_connection()):
        pass

    app = (
        Application.builder().token(token).concurrent_updates(4)
        .post_init(configure_commands).post_shutdown(stop_reaction_listener).build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("artists", artists_command))
    app.add_handler(CommandHandler("renametopic", rename_topic_command))
    app.add_handler(CommandHandler("zip", zip_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("findout", findout_command))
    app.add_handler(CommandHandler(["favorite", "unfavorite"], change_favorite))
    app.add_handler(CommandHandler("favorites", favorites_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("share", share_command))
    app.add_handler(CommandHandler("unshare", unshare_command))
    app.add_handler(CallbackQueryHandler(
        shared_playlist_action,
        pattern=r"^shr:[A-Za-z0-9_-]{32}:(?:a:\d+|g:\d+:\d+|s:[A-Za-z0-9_-]{11})$",
    ))
    app.add_handler(CallbackQueryHandler(zip_archive_action, pattern=r"^zip:(?:own|[A-Za-z0-9_-]{32})$"))
    app.add_handler(CallbackQueryHandler(stop_zip_archive, pattern=r"^zipstop:[a-f0-9]{12}$"))
    app.add_handler(CallbackQueryHandler(close_search, pattern=r"^searchclose:\d+(?::[a-f0-9]{12})?$"))
    app.add_handler(CallbackQueryHandler(search_song_action, pattern=r"^searchsong:[a-f0-9]{12}:\d+$"))
    app.add_handler(CallbackQueryHandler(favorites_action, pattern=r"^fav:\d+:(?:\d+|close)$"))
    app.add_handler(CallbackQueryHandler(stats_action, pattern=r"^stats:\d+:(?:\d+|close)$"))
    app.add_handler(CallbackQueryHandler(findout_action, pattern=r"^find:[a-f0-9]{12}:(?:\d+|download|close)$"))
    app.add_handler(CallbackQueryHandler(cancel_download, pattern=r"^cancel:[a-f0-9]{12}$"))
    app.add_handler(CallbackQueryHandler(retry_download, pattern=r"^retry:[a-f0-9]{12}:(?:run|close)$"))
    app.add_handler(CallbackQueryHandler(
        library_action,
        pattern=r"^lib:[a-f0-9]{12}:(?:artist:\d+|song:\d+|ap:\d+|sp:\d+|back:0)$",
    ))
    app.add_handler(CallbackQueryHandler(
        select_playlist,
        pattern=r"^mix:[a-f0-9]{12}:(?:toggle:\d+|page:\d+|select:(?:all|none)|remember:now|more:next|why:(?:show|back)|close:now|auto:mp3|download:(?:mp3|mp4)|quality:(?:(?:mp3:(?:128|192|320))|(?:mp4:(?:360|480|720))|back))$",
    ))
    app.add_handler(CallbackQueryHandler(
        send_media,
        pattern=r"^(?:(?:mp3|mp4):[a-f0-9]{12}|format:[a-f0-9]{12}:(?:back|close)|quality:[a-f0-9]{12}:(?:mp3:(?:128|192|320)|mp4:(?:360|480|720)))$",
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    LOG.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
