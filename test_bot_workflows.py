import io
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from mutagen.id3 import APIC, ID3


class FakeBot:
    def __init__(self):
        self.sent = []
        self.deleted = []
        self.reactions = []

    async def get_me(self):
        return SimpleNamespace(has_topics_enabled=False)

    async def send_audio(self, **kwargs):
        self.sent.append(kwargs)
        number = len(self.sent)
        return SimpleNamespace(message_id=100 + number, audio=SimpleNamespace(file_id=f"file-{number}"))

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs["message_id"])

    async def set_message_reaction(self, **kwargs):
        self.reactions.append(kwargs)


class FakeQuery:
    def __init__(self, data="", on_edit=None):
        self.data = data
        self.from_user = SimpleNamespace(id=42)
        self.message = SimpleNamespace(chat_id=7, message_id=99, chat=SimpleNamespace(type="private"))
        self.edits = []
        self.on_edit = on_edit

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))
        if self.on_edit:
            self.on_edit()

    async def answer(self, *args, **kwargs):
        return None


class BotWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "history.sqlite3"
        original = bot.history_connection
        self.connection_patch = patch.object(
            bot, "history_connection", side_effect=lambda db_path=None: original(self.db)
        )
        self.connection_patch.start()
        self.fake_bot = FakeBot()
        self.context = SimpleNamespace(
            bot=self.fake_bot, application=SimpleNamespace(bot_data={}), user_data={}
        )

    async def asyncTearDown(self):
        self.connection_patch.stop()
        self.directory.cleanup()

    async def test_retry_only_failed_url(self):
        first = "https://www.youtube.com/watch?v=video000001"
        second = "https://www.youtube.com/watch?v=video000002"
        failed_once = {second}

        def download(url, media_type, directory, quality):
            self.assertEqual(quality, "320")
            if url in failed_once:
                raise ValueError("temporary failure")
            path = directory / "song.mp3"
            path.write_bytes(b"audio")
            return path, "Artist", url[-11:], None

        job_id = "0123456789ab"
        query = FakeQuery()
        with patch.object(bot, "download_media", side_effect=download):
            await bot.deliver_media(
                query, self.context, [first, second], "mp3", 7, 88, job_id=job_id,
                quality="320",
            )
            retry = bot.load_retry_batch(job_id)
            self.assertEqual(retry["urls"], [second])
            self.assertEqual(retry["quality"], "320")
            self.assertEqual(len(self.fake_bot.sent), 1)
            self.assertIn("Повторить неудачные", query.edits[-1][1].inline_keyboard[0][0].text)

            failed_once.clear()
            retry_query = FakeQuery(f"retry:{job_id}:run")
            restarted_context = SimpleNamespace(
                bot=self.fake_bot, application=SimpleNamespace(bot_data={}), user_data={}
            )
            await bot.retry_download(SimpleNamespace(callback_query=retry_query), restarted_context)

        self.assertEqual(len(self.fake_bot.sent), 2)
        self.assertIsNone(bot.load_retry_batch(job_id))
        self.assertIn(88, self.fake_bot.deleted)
        self.assertIn(99, self.fake_bot.deleted)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sent_tracks").fetchone()[0], 2)

    async def test_cancel_before_sending_file(self):
        job_id = "abcdef012345"

        def download(url, media_type, directory, quality):
            path = directory / "song.mp3"
            path.write_bytes(b"audio")
            return path, "Artist", "Song", None

        query = FakeQuery(on_edit=lambda: self.context.application.bot_data["active_batches"][job_id].update(
            cancelled=True
        ))
        with patch.object(bot, "download_media", side_effect=download):
            await bot.deliver_media(
                query, self.context, ["https://www.youtube.com/watch?v=video000003"],
                "mp3", 7, 88, job_id=job_id,
            )
        self.assertFalse(self.fake_bot.sent)
        self.assertEqual(self.fake_bot.deleted, [88, 99])
        self.assertNotIn(job_id, self.context.application.bot_data["active_batches"])

    async def test_search_uses_topic_threshold(self):
        for number in range(4):
            bot.record_sent_track(
                7, f"https://www.youtube.com/watch?v=video{number:06d}",
                "Hozier", f"Song {number}", self.db,
            )
        results = []
        async def reply_text(text, **kwargs):
            results.append(text)
        message = SimpleNamespace(message_id=55, reply_text=reply_text)
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=7), effective_user=SimpleNamespace(id=42), effective_message=message)
        context = SimpleNamespace(args=["Hozier"], bot=self.fake_bot)
        await bot.search_command(update, context)
        self.assertIn("→ NoName", results[0])
        self.assertEqual(len(self.fake_bot.deleted), 1)

    async def test_cover_is_embedded_in_mp3(self):
        source = Path(self.directory.name) / "source.jpg"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
             "color=c=red:s=640x480:d=1", "-frames:v", "1", str(source)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with patch.object(bot, "urlopen", return_value=io.BytesIO(source.read_bytes())):
            cover = bot.youtube_cover(
                "https://www.youtube.com/watch?v=video000004", Path(self.directory.name)
            )
        self.assertIsNotNone(cover)
        self.assertLess(cover.stat().st_size, 200_000)
        audio = Path(self.directory.name) / "song.mp3"
        audio.write_bytes(b"")
        bot.tag_mp3(audio, "Artist", "Song", cover)
        self.assertTrue(ID3(audio).getall("APIC"))

    async def test_same_song_from_different_video_is_skipped(self):
        first = "https://www.youtube.com/watch?v=video000011"
        second = "https://www.youtube.com/watch?v=video000012"
        bot.record_sent_track(7, first, "Скриптонит & Feduk", "Положение (Official Video)", self.db)
        self.assertTrue(bot.already_sent(7, second, bot.song_key("Скриптонит", "Положение")))
        available, skipped = bot.filter_sent_tracks(
            7, [(second, "Скриптонит - Положение", bot.song_key("Скриптонит", "Положение"))], self.db
        )
        self.assertEqual((available, skipped), ([], 1))
        self.assertNotEqual(
            bot.song_key("Скриптонит", "Положение (Live)"),
            bot.song_key("Скриптонит", "Положение"),
        )

    async def test_favorite_marks_existing_message_without_sending_audio(self):
        url = "https://www.youtube.com/watch?v=video000013"
        bot.record_sent_track(7, url, "Hozier", "Too Sweet", self.db, message_id=123)
        deleted = []
        async def delete():
            deleted.append(True)
        message = SimpleNamespace(
            chat_id=7, text="/favorite", reply_to_message=SimpleNamespace(
                message_id=123, audio=SimpleNamespace(), video=None
            ), delete=delete,
        )
        await bot.change_favorite(SimpleNamespace(effective_message=message), self.context)
        self.assertEqual(bot.favorite_tracks(7, self.db), [("Hozier", "Too Sweet")])
        self.assertEqual(self.fake_bot.reactions[0]["reaction"], "❤️")
        self.assertFalse(self.fake_bot.sent)
        self.assertTrue(deleted)
        message.text = "/unfavorite"
        await bot.change_favorite(SimpleNamespace(effective_message=message), self.context)
        self.assertEqual(bot.favorite_tracks(7, self.db), [])

    async def test_next_thirty_keeps_selection(self):
        def entries(start):
            return [
                (f"https://www.youtube.com/watch?v=video{n:06d}", f"Artist - Song {n}",
                 bot.song_key("Artist", f"Song {n}"))
                for n in range(start, start + 30)
            ]
        job_id = "123456789abc"
        job = {
            "url": "https://www.youtube.com/playlist?list=RDvideo000001",
            "title": "Mix", "entries": entries(1), "selected": {0, 29}, "page": 0,
            "skipped": 0, "reasons": [], "has_more": True, "next_start": 31,
            "source_chat_id": 7, "source_message_id": 88,
        }
        self.context.user_data["mixes"] = {job_id: job}
        query = FakeQuery(f"mix:{job_id}:more:next")
        with patch.object(bot, "list_playlist", return_value=("Mix", entries(31), False)) as listing:
            await bot.select_playlist(SimpleNamespace(callback_query=query), self.context)
        listing.assert_called_once_with(job["url"], 31)
        self.assertEqual(len(job["entries"]), 60)
        self.assertEqual(job["selected"], {0, 29})
        self.assertEqual(job["page"], 3)
        self.assertFalse(job["has_more"])

    async def test_hidden_track_explains_exact_match(self):
        old = "https://www.youtube.com/watch?v=video000041"
        new = "https://www.youtube.com/watch?v=video000042"
        bot.record_sent_track(7, old, "Скриптонит & Feduk", "Положение (Official Video)", self.db)
        available, reasons = bot.filter_sent_tracks_explained(7, [
            (new, "Скриптонит - Положение (Lyrics)", bot.song_key("Скриптонит", "Положение")),
        ], self.db)
        self.assertEqual(available, [])
        self.assertIn("Положение (Official Video)", reasons[0][1])
        self.assertEqual(reasons[0][2], "в чате")

    async def test_stats_shows_artist_threshold(self):
        for number in range(4):
            bot.record_sent_track(
                7, f"https://www.youtube.com/watch?v=video{number + 50:06d}",
                "Hozier", f"Song {number}", self.db,
            )
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=video000060", "Скриптонит & Feduk", "Положение", self.db,
        )
        text, _ = bot.stats_view(7, 42, 0)
        self.assertIn("Песен: 5", text)
        self.assertIn("Hozier: 4 · до своей темы 1", text)
        self.assertIn("Скриптонит: 1 · до своей темы 4", text)

    async def test_quality_choice_for_direct_link(self):
        job_id = "111111111111"
        self.context.user_data["jobs"] = {
            job_id: (["https://www.youtube.com/watch?v=video000061"], 7, 88)
        }
        chooser = FakeQuery(f"mp4:{job_id}")
        await bot.send_media(SimpleNamespace(callback_query=chooser), self.context)
        labels = [button.text for row in chooser.edits[-1][1].inline_keyboard for button in row]
        self.assertIn("720 p", labels)
        downloader = FakeQuery(f"quality:{job_id}:mp4:720")
        with patch.object(bot, "deliver_media", new_callable=AsyncMock) as deliver:
            await bot.send_media(SimpleNamespace(callback_query=downloader), self.context)
        self.assertEqual(deliver.await_args.kwargs["quality"], "720")
        self.assertNotIn(job_id, self.context.user_data["jobs"])

    async def test_quality_choice_for_mix(self):
        job_id = "222222222222"
        job = {
            "title": "Mix", "entries": [("https://www.youtube.com/watch?v=video000062", "Artist - Song", "artist|song")],
            "selected": {0}, "page": 0, "skipped": 0, "reasons": [], "has_more": False,
            "source_chat_id": 7, "source_message_id": 88,
        }
        self.context.user_data["mixes"] = {job_id: job}
        chooser = FakeQuery(f"mix:{job_id}:download:mp3")
        await bot.select_playlist(SimpleNamespace(callback_query=chooser), self.context)
        self.assertIn("320 кбит/с", [button.text for row in chooser.edits[-1][1].inline_keyboard for button in row])
        downloader = FakeQuery(f"mix:{job_id}:quality:mp3:128")
        with patch.object(bot, "deliver_media", new_callable=AsyncMock) as deliver:
            await bot.select_playlist(SimpleNamespace(callback_query=downloader), self.context)
        self.assertEqual(deliver.await_args.kwargs["quality"], "128")

    async def test_download_options_match_selected_quality(self):
        options = []
        directory = Path(self.directory.name)

        class FakeYoutubeDL:
            def __init__(self, settings):
                options.append(settings)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def extract_info(self, url, download):
                if download:
                    (directory / "result.mp4").write_bytes(b"video")
                return {"title": "Artist - Song"}

        with patch.object(bot, "YoutubeDL", FakeYoutubeDL):
            bot.download_media("https://www.youtube.com/watch?v=video000063", "mp4", directory, "720")
        self.assertIn("height<=720", options[0]["format"])

    async def test_old_retry_batch_defaults_to_previous_quality(self):
        with closing(sqlite3.connect(self.db)) as connection:
            with connection:
                connection.execute(
                    "CREATE TABLE retry_batches (job_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
                    "chat_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL, media_type TEXT NOT NULL, "
                    "urls_json TEXT NOT NULL, known_keys_json TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO retry_batches VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("333333333333", 42, 7, 88, "mp4", "[]", "{}"),
                )
        job = bot.load_retry_batch("333333333333", self.db)
        self.assertEqual(job["quality"], "480")
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertIn("quality", [row[1] for row in connection.execute("PRAGMA table_info(retry_batches)")])

    async def test_auto_mix_skips_saved_songs_and_downloads_next_thirty(self):
        def entry(number):
            return (
                f"https://www.youtube.com/watch?v=video{number:06d}",
                f"Artist - Song {number}", bot.song_key("Artist", f"Song {number}"),
            )
        for number in range(1, 31):
            bot.record_sent_track(7, entry(number)[0], "Artist", f"Song {number}", self.db)
        job_id = "444444444444"
        job = {
            "url": "https://www.youtube.com/playlist?list=RDvideo000001",
            "title": "Mix", "entries": [entry(number) for number in range(1, 31)],
            "selected": set(), "page": 0, "skipped": 0, "reasons": [],
            "has_more": True, "next_start": 31, "source_chat_id": 7, "source_message_id": 88,
        }
        self.context.user_data["mixes"] = {job_id: job}
        query = FakeQuery(f"mix:{job_id}:auto:mp3")
        with patch.object(bot, "list_playlist", return_value=(
            "Mix", [entry(number) for number in range(31, 61)], True,
        )) as listing, patch.object(bot, "deliver_media", new_callable=AsyncMock) as deliver:
            await bot.select_playlist(SimpleNamespace(callback_query=query), self.context)
        listing.assert_called_once_with(job["url"], 31)
        self.assertEqual(len(deliver.await_args.args[2]), 30)
        self.assertEqual(deliver.await_args.args[2][0], entry(31)[0])
        self.assertEqual(deliver.await_args.kwargs["quality"], "192")
        self.assertNotIn(job_id, self.context.user_data["mixes"])

    async def test_auto_mix_stops_after_150_inspected_positions(self):
        job = {
            "url": "https://www.youtube.com/playlist?list=RDvideo000001",
            "entries": [], "next_start": 31, "has_more": True, "source_chat_id": 7,
        }
        with patch.object(bot, "list_playlist", return_value=("Mix", [], True)) as listing:
            selected, checked = await bot.collect_auto_playlist(job)
        self.assertEqual(selected, [])
        self.assertEqual(checked, 150)
        self.assertEqual([call.args[1] for call in listing.call_args_list], [31, 61, 91, 121])

    async def test_auto_mix_with_no_new_songs_keeps_menu(self):
        url = "https://www.youtube.com/watch?v=video000070"
        bot.record_sent_track(7, url, "Artist", "Song", self.db)
        job_id = "555555555555"
        job = {
            "url": "https://www.youtube.com/playlist?list=RDvideo000070",
            "title": "Mix", "entries": [(url, "Artist - Song", bot.song_key("Artist", "Song"))],
            "selected": set(), "page": 0, "skipped": 0, "reasons": [],
            "has_more": False, "next_start": 31, "source_chat_id": 7, "source_message_id": 88,
        }
        self.context.user_data["mixes"] = {job_id: job}
        query = FakeQuery(f"mix:{job_id}:auto:mp3")
        with patch.object(bot, "deliver_media", new_callable=AsyncMock) as deliver:
            await bot.select_playlist(SimpleNamespace(callback_query=query), self.context)
        deliver.assert_not_awaited()
        self.assertIn("новых песен нет", query.edits[-1][0])
        self.assertIn(job_id, self.context.user_data["mixes"])

    async def test_youtube_search_keeps_video_results_and_duration(self):
        options = []

        class FakeYoutubeDL:
            def __init__(self, settings):
                options.append(settings)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def extract_info(self, url, download):
                self.query = url
                self.download = download
                return {"entries": [
                    {"id": "video000071", "title": "Jace June - Come Home", "duration": 169},
                    {"id": "invalid-id", "title": "Channel"},
                ]}

        with patch.object(bot, "YoutubeDL", FakeYoutubeDL):
            results = bot.search_youtube("Jace June Come Home")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "https://www.youtube.com/watch?v=video000071")
        self.assertEqual(results[0][3], 169)
        self.assertTrue(options[0]["extract_flat"])

    async def test_findout_result_goes_through_format_and_quality(self):
        results = [(
            "https://www.youtube.com/watch?v=video000072",
            "Jace June - Come Home", bot.song_key("Jace June", "Come Home"), 169,
        )]
        edits = []
        async def edit_text(text, **kwargs):
            edits.append((text, kwargs.get("reply_markup")))
        async def reply_text(text, **kwargs):
            return SimpleNamespace(edit_text=edit_text)
        message = SimpleNamespace(chat_id=7, message_id=88, reply_text=reply_text)
        update = SimpleNamespace(
            effective_message=message, effective_user=SimpleNamespace(id=42),
        )
        self.context.args = ["Jace", "June", "Come", "Home"]
        with patch.object(bot, "search_youtube", return_value=results):
            await bot.findout_command(update, self.context)
        self.assertIn("[2:49]", edits[-1][1].inline_keyboard[0][0].text)
        job_id = next(iter(self.context.user_data["youtube_searches"]))
        pick = FakeQuery(f"find:{job_id}:0")
        await bot.findout_action(SimpleNamespace(callback_query=pick), self.context)
        self.assertIn("Теперь выберите формат", pick.edits[-1][0])
        choose_format = FakeQuery(f"mp3:{job_id}")
        await bot.send_media(SimpleNamespace(callback_query=choose_format), self.context)
        choose_quality = FakeQuery(f"quality:{job_id}:mp3:192")
        with patch.object(bot, "deliver_media", new_callable=AsyncMock) as deliver:
            await bot.send_media(SimpleNamespace(callback_query=choose_quality), self.context)
        self.assertEqual(deliver.await_args.args[2], [results[0][0]])
        self.assertEqual(deliver.await_args.args[5], 88)
        self.assertEqual(deliver.await_args.kwargs["quality"], "192")

    async def test_findout_saved_result_cannot_download_again(self):
        url = "https://www.youtube.com/watch?v=video000073"
        bot.record_sent_track(7, url, "Artist", "Song", self.db)
        job_id = "666666666666"
        self.context.user_data["youtube_searches"] = {job_id: {
            "entries": [(url, "Artist - Song", bot.song_key("Artist", "Song"), 180)],
            "user_id": 42, "chat_id": 7, "source_message_id": 88,
        }}
        query = FakeQuery(f"find:{job_id}:0")
        await bot.findout_action(SimpleNamespace(callback_query=query), self.context)
        self.assertFalse(query.edits)
        self.assertFalse(self.context.user_data.get("jobs"))

    async def test_findout_close_removes_search_and_messages(self):
        job_id = "777777777777"
        self.context.user_data["youtube_searches"] = {job_id: {
            "entries": [], "user_id": 42, "chat_id": 7, "source_message_id": 88,
        }}
        query = FakeQuery(f"find:{job_id}:close")
        await bot.findout_action(SimpleNamespace(callback_query=query), self.context)
        self.assertEqual(self.fake_bot.deleted, [88, 99])
        self.assertNotIn(job_id, self.context.user_data["youtube_searches"])


if __name__ == "__main__":
    unittest.main()
