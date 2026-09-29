"""Telegram bot that returns a YouTube video as MP3 or MP4."""

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
from contextlib import ExitStack, closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from dotenv import load_dotenv
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TIT2, TPE1
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


LOG = logging.getLogger(__name__)
HISTORY_DB = Path(__file__).with_name("downloads.sqlite3")
MAX_FILE_BYTES = 50_000_000  # Telegram Bot API upload limit
MAX_BATCH = 10
PLAYLIST_LIMIT = 30
PLAYLIST_PAGE_SIZE = 8
MAX_PLAYLIST_SELECTION = 150
ARTIST_TOPIC_THRESHOLD = 5
MISC_TOPIC_NAME = "NoName"
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
        artist = artist or from_title.strip()
        track = track or from_title_track.strip()
    track = str(track or title).strip()
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
    """Put collaborations in the first named artist's catalog section."""
    first = re.split(
        r"\s*[,;&×+]\s*|\s+(?:x|feat\.?|ft\.?|featuring|with)\s+",
        artist, maxsplit=1, flags=re.IGNORECASE,
    )[0]
    first = re.sub(r"^official\s+|\s*vevo$|\s*-\s*topic$", "", first, flags=re.IGNORECASE)
    return first.strip() or artist


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


def favorite_tracks(chat_id: int, db_path: Path = HISTORY_DB) -> list[tuple[str, str]]:
    with closing(history_connection(db_path)) as connection:
        rows = connection.execute(
            "SELECT artist, title, song_key, video_id FROM sent_tracks "
            "WHERE chat_id = ? AND favorite = 1 ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE",
            (chat_id,),
        ).fetchall()
    return [
        (artist or key.partition("|")[0] or "Без исполнителя", title or key.partition("|")[2] or video_id)
        for artist, title, key, video_id in rows
    ]


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
        if unicodedata.normalize("NFKC", primary_artist(source_artist)).casefold() != target:
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
        except (TelegramError, sqlite3.Error, OSError) as exc:
            LOG.warning("Could not move track %s to artist topic: %s", video_id, exc)


def library_groups(chat_id: int, db_path: Path = HISTORY_DB) -> list[tuple[str, list[dict]]]:
    """Return saved songs grouped by artist, with repeated songs shown once."""
    with closing(history_connection(db_path)) as connection:
        rows = connection.execute(
            "SELECT video_id, song_key, artist, title, audio_file_id "
            "FROM sent_tracks WHERE chat_id = ? "
            "ORDER BY (audio_file_id IS NOT NULL) DESC, sent_at DESC", (chat_id,)
        ).fetchall()
    groups = {}
    seen_songs = set()
    for video_id, key, artist, title, audio_file_id in rows:
        fallback_artist, _, fallback_title = key.partition("|")
        artist = artist or fallback_artist or "Без исполнителя"
        title = title or fallback_title or video_id
        identity = key or video_id
        if identity in seen_songs:
            continue
        seen_songs.add(identity)
        group_artist = primary_artist(artist)
        group_key = unicodedata.normalize("NFKC", group_artist).casefold()
        group = groups.setdefault(group_key, [group_artist, []])
        group[1].append({
            "video_id": video_id,
            "artist": artist,
            "title": title,
            "audio_file_id": audio_file_id,
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
    await update.effective_message.reply_text(
        "Пришлите одну или несколько ссылок на видео YouTube (до 10 в одном сообщении). "
        "Если пришлёте ссылку на Mix, можно будет выбрать треки из списка. "
        "Я предложу скачать MP3 или MP4 и выбрать качество. "
        "Файл должен быть не больше 50 МБ. Во время загрузки можно нажать «Отменить», "
        "а неудачные ссылки повторить кнопкой. /artists — разделы музыки, /search — поиск, "
        "/stats — статистика. "
        "Ответьте /favorite на песню, чтобы добавить её в /favorites."
    )


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
    return text


def quality_markup(job_id: str, media_type: str, *, mix: bool = False) -> InlineKeyboardMarkup:
    unit = "кбит/с" if media_type == "mp3" else "p"
    prefix = f"mix:{job_id}:quality:{media_type}:" if mix else f"quality:{job_id}:{media_type}:"
    rows = [[InlineKeyboardButton(
        f"{quality} {unit}", callback_data=f"{prefix}{quality}"
    )] for quality in QUALITY_OPTIONS[media_type]]
    if mix:
        rows.append([InlineKeyboardButton("◀️ К списку", callback_data=f"mix:{job_id}:quality:back")])
    else:
        rows.append([InlineKeyboardButton("◀️ К форматам", callback_data=f"format:{job_id}:back")])
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
        await deliver_media(
            query, context, urls, media_type, job["source_chat_id"], job["source_message_id"],
            known_keys, job_id=job_id, quality=quality,
        )
        return
    await query.answer()
    await query.edit_message_text(playlist_text(job), reply_markup=playlist_markup(job_id, job))


async def send_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        parts = query.data.split(":")
        if len(parts) == 2 and parts[0] in QUALITY_OPTIONS:
            media_type, job_id = parts
            job = context.user_data.setdefault("jobs", {}).get(job_id)
            if job is None:
                await query.answer("Эта кнопка устарела. Пришлите ссылку снова.", show_alert=True)
                return
            await query.answer()
            await query.edit_message_text(
                f"Выберите качество {media_type.upper()}:",
                reply_markup=quality_markup(job_id, media_type),
            )
            return
        if len(parts) == 3 and parts[0] == "format" and parts[2] == "back":
            job_id = parts[1]
            if job_id not in context.user_data.setdefault("jobs", {}):
                await query.answer("Эта кнопка устарела.", show_alert=True)
                return
            await query.answer()
            await query.edit_message_text(
                "Выберите формат для всех ссылок:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎵 MP3", callback_data=f"mp3:{job_id}"),
                    InlineKeyboardButton("🎬 MP4", callback_data=f"mp4:{job_id}"),
                ]]),
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

    job = context.user_data.setdefault("jobs", {}).pop(job_id, None)
    if job is None:
        await query.answer("Эта кнопка уже использована. Пришлите ссылку снова.", show_alert=True)
        return
    await query.answer()
    urls, source_chat_id, source_message_id = job
    await deliver_media(
        query, context, urls, media_type, source_chat_id, source_message_id,
        job_id=job_id, quality=quality,
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
    await deliver_media(
        query, context, job["urls"], job["media_type"], job["chat_id"],
        job["source_message_id"], job["known_keys"], job_id=job_id, quality=job["quality"],
    )


async def deliver_media(
    query, context, urls, media_type, source_chat_id, source_message_id,
    known_keys=None, *, job_id=None, quality=None,
) -> None:
    job_id = job_id or uuid4().hex[:12]
    quality = quality or DEFAULT_QUALITY[media_type]
    known_keys = known_keys or {}
    active = context.application.bot_data.setdefault("active_batches", {})
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
                with tempfile.TemporaryDirectory(prefix="telega-") as temp:
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
                            count = artist_track_count(source_chat_id, artist or "Без исполнителя")
                            if count + 1 >= ARTIST_TOPIC_THRESHOLD:
                                thread = await artist_topic(context, source_chat_id, artist)
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
                                    context, source_chat_id, artist or "Без исполнителя", misc_thread, thread,
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
    session = {"groups": groups, "artist_index": None, "artist_page": 0, "song_page": 0}
    sessions[job_id] = session
    await update.effective_message.reply_text(
        library_text(session), reply_markup=library_markup(job_id, session)
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_text = " ".join(context.args).strip()
    if not query_text:
        text = "Напишите /search и имя исполнителя или название песни."
    else:
        terms = unicodedata.normalize("NFKC", query_text).casefold().split()
        try:
            groups = library_groups(update.effective_chat.id)
        except (sqlite3.Error, OSError) as exc:
            LOG.warning("Could not search library: %s", exc)
            groups = []
        matches = []
        for artist, tracks in groups:
            topic = artist if len(tracks) >= ARTIST_TOPIC_THRESHOLD else MISC_TOPIC_NAME
            for track in tracks:
                label = f"{track['artist']} — {track['title']}"
                searchable = unicodedata.normalize("NFKC", label).casefold()
                if all(term in searchable for term in terms):
                    matches.append((label, topic))
        if matches:
            lines = [f"🔎 Найдено: {len(matches)}. Откройте указанную тему:"]
            for label, topic in matches[:15]:
                lines.append(f"• {label} → {topic}")
            if len(matches) > 15:
                lines.append("Показаны первые 15. Уточните запрос.")
            text = "\n".join(lines)
        else:
            text = "По этому запросу песен не найдено."
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✖️ Закрыть", callback_data=f"searchclose:{update.effective_user.id}"),
    ]])
    await update.effective_message.reply_text(text[:4000], reply_markup=markup)
    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id, message_id=update.effective_message.message_id,
        )
    except TelegramError:
        pass


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
        found = set_favorite(message.chat_id, reply.message_id, enabled)
    except (sqlite3.Error, OSError) as exc:
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
    user_id = int(query.data.split(":", 1)[1])
    if query.from_user.id != user_id:
        await query.answer("Это чужой поиск.", show_alert=True)
        return
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
        session = {"groups": groups, "artist_index": None, "artist_page": 0, "song_page": 0}
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
            BotCommand("search", "Найти песню или исполнителя"),
            BotCommand("favorite", "Добавить песню в избранное (ответом)"),
            BotCommand("unfavorite", "Удалить песню из избранного (ответом)"),
            BotCommand("favorites", "Мои избранные песни"),
            BotCommand("stats", "Статистика песен и исполнителей"),
        ])
    except TelegramError as exc:
        LOG.warning("Could not update bot commands: %s", exc)


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

    app = Application.builder().token(token).concurrent_updates(4).post_init(configure_commands).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("artists", artists_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler(["favorite", "unfavorite"], change_favorite))
    app.add_handler(CommandHandler("favorites", favorites_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(close_search, pattern=r"^searchclose:\d+$"))
    app.add_handler(CallbackQueryHandler(favorites_action, pattern=r"^fav:\d+:(?:\d+|close)$"))
    app.add_handler(CallbackQueryHandler(stats_action, pattern=r"^stats:\d+:(?:\d+|close)$"))
    app.add_handler(CallbackQueryHandler(cancel_download, pattern=r"^cancel:[a-f0-9]{12}$"))
    app.add_handler(CallbackQueryHandler(retry_download, pattern=r"^retry:[a-f0-9]{12}:(?:run|close)$"))
    app.add_handler(CallbackQueryHandler(
        library_action,
        pattern=r"^lib:[a-f0-9]{12}:(?:artist:\d+|song:\d+|ap:\d+|sp:\d+|back:0)$",
    ))
    app.add_handler(CallbackQueryHandler(
        select_playlist,
        pattern=r"^mix:[a-f0-9]{12}:(?:toggle:\d+|page:\d+|select:(?:all|none)|remember:now|more:next|why:(?:show|back)|close:now|download:(?:mp3|mp4)|quality:(?:(?:mp3:(?:128|192|320))|(?:mp4:(?:360|480|720))|back))$",
    ))
    app.add_handler(CallbackQueryHandler(
        send_media,
        pattern=r"^(?:(?:mp3|mp4):[a-f0-9]{12}|format:[a-f0-9]{12}:back|quality:[a-f0-9]{12}:(?:mp3:(?:128|192|320)|mp4:(?:360|480|720)))$",
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    LOG.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
