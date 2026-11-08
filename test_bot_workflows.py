import asyncio
import io
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
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
        self.topics_enabled = False
        self.topics = []
        self.edits = []
        self.topic_edits = []
        self.copies = []
        self.files = {}
        self.documents = []
        self.messages = []

    async def get_me(self):
        return SimpleNamespace(has_topics_enabled=self.topics_enabled, username="musica_test_bot")

    async def create_forum_topic(self, **kwargs):
        self.topics.append(kwargs)
        return SimpleNamespace(message_thread_id=500 + len(self.topics))

    async def edit_forum_topic(self, **kwargs):
        self.topic_edits.append(kwargs)
        return True

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=400 + len(self.messages))

    async def get_file(self, file_id, **kwargs):
        value = self.files[file_id]
        if isinstance(value, Exception):
            raise value
        content, advertised_size = value if isinstance(value, tuple) else (value, len(value))

        async def download_to_drive(custom_path, **kwargs):
            Path(custom_path).write_bytes(content)
            return Path(custom_path)

        return SimpleNamespace(file_size=advertised_size, download_to_drive=download_to_drive)

    async def send_document(self, **kwargs):
        self.documents.append({**kwargs, "content": kwargs["document"].read()})
        return SimpleNamespace(message_id=500 + len(self.documents))

    async def send_audio(self, **kwargs):
        self.sent.append(kwargs)
        number = len(self.sent)
        return SimpleNamespace(message_id=100 + number, audio=SimpleNamespace(file_id=f"file-{number}"))

    async def copy_message(self, **kwargs):
        self.copies.append(kwargs)
        return SimpleNamespace(message_id=600 + len(self.copies))

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
        self.edit_kwargs = []
        self.answers = []
        self.on_edit = on_edit

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))
        self.edit_kwargs.append(kwargs)
        if self.on_edit:
            self.on_edit()

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))
        return None


class FakeApplication:
    def __init__(self):
        self.bot_data = {}
        self.tasks = []
        self.running = True
        self.bot = None

    def create_task(self, work, update=None):
        task = asyncio.create_task(work)
        self.tasks.append(task)
        return task


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
            bot=self.fake_bot, application=FakeApplication(), user_data={}
        )
        self.context.application.bot = self.fake_bot

    async def asyncTearDown(self):
        self.connection_patch.stop()
        self.directory.cleanup()

    def private_command_update(self, chat_id=7, chat_type="private"):
        message = SimpleNamespace(message_id=200, reply_text=AsyncMock(return_value=SimpleNamespace(message_id=300)))
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
            effective_message=message,
            effective_user=SimpleNamespace(id=chat_id),
        )
        return update, message

    async def test_rename_topic_keeps_thread_mapping(self):
        with closing(bot.history_connection()) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO artist_topics VALUES (7, 'shiza', 123)"
                )
        update, message = self.private_command_update()
        message.message_thread_id = 123
        self.context.args = ["Моя", "музыка"]
        await bot.rename_topic_command(update, self.context)
        self.assertEqual(self.fake_bot.topic_edits, [
            {"chat_id": 7, "message_thread_id": 123, "name": "Моя музыка"}
        ])
        message.reply_text.assert_awaited_once_with("Тема переименована: Моя музыка")
        with closing(bot.history_connection()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT message_thread_id FROM artist_topics WHERE chat_id=7 AND artist_key='shiza'"
                ).fetchone()[0],
                123,
            )

    async def test_rename_topic_requires_own_private_thread_and_valid_name(self):
        update, message = self.private_command_update()
        self.context.args = ["Название"]
        await bot.rename_topic_command(update, self.context)
        message.reply_text.assert_awaited_once()
        self.assertIn("Откройте", message.reply_text.await_args.args[0])

        update, message = self.private_command_update(chat_type="group")
        message.message_thread_id = 123
        await bot.rename_topic_command(update, self.context)
        self.assertIn("только", message.reply_text.await_args.args[0])

        update, message = self.private_command_update()
        update.effective_user.id = 999
        message.message_thread_id = 123
        await bot.rename_topic_command(update, self.context)
        self.assertIn("только", message.reply_text.await_args.args[0])

        update, message = self.private_command_update()
        message.message_thread_id = 123
        self.context.args = []
        await bot.rename_topic_command(update, self.context)
        self.assertIn("128", message.reply_text.await_args.args[0])

        update, message = self.private_command_update()
        message.message_thread_id = 123
        self.context.args = ["x" * 129]
        await bot.rename_topic_command(update, self.context)
        self.assertIn("128", message.reply_text.await_args.args[0])
        self.assertEqual(self.fake_bot.topic_edits, [])

    async def test_rename_topic_reports_telegram_failure(self):
        update, message = self.private_command_update()
        message.message_thread_id = 123
        self.context.args = ["Новое", "название"]
        self.fake_bot.edit_forum_topic = AsyncMock(side_effect=bot.TelegramError("topic missing"))
        await bot.rename_topic_command(update, self.context)
        self.assertIn("Не удалось", message.reply_text.await_args.args[0])

    async def test_rename_topic_command_and_reply_disappear_after_delay(self):
        update, message = self.private_command_update()
        message.message_thread_id = 123
        self.context.args = ["ИК"]
        with patch.object(bot, "TRANSIENT_REPLY_SECONDS", 0):
            await bot.rename_topic_command(update, self.context)
            await asyncio.sleep(0.01)
        self.assertEqual(self.fake_bot.deleted, [200, 300])

        self.fake_bot.deleted.clear()
        self.context.args = []
        with patch.object(bot, "TRANSIENT_REPLY_SECONDS", 0):
            await bot.rename_topic_command(update, self.context)
            await asyncio.sleep(0.01)
        self.assertEqual(self.fake_bot.deleted, [200, 300])

    async def test_transient_cleanup_retries_telegram_delay_and_network_error(self):
        bot.queue_transient_cleanup(7, (401,), 0)
        self.fake_bot.delete_message = AsyncMock(side_effect=[
            bot.RetryAfter(1), bot.NetworkError("temporary"), None,
        ])
        with patch.object(bot.asyncio, "sleep", new=AsyncMock()) as sleep:
            await bot.delete_transient_messages(self.fake_bot, 7, (401,), 0)
        self.assertEqual(self.fake_bot.delete_message.await_count, 3)
        self.assertIn(1, [call.args[0] for call in sleep.await_args_list])
        self.assertIn(2, [call.args[0] for call in sleep.await_args_list])
        with closing(bot.history_connection()) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM transient_message_cleanup"
            ).fetchone()[0], 0)

    async def test_transient_cleanup_limits_retry_burst_and_keeps_pending_record(self):
        bot.queue_transient_cleanup(7, (401,), 0)
        self.fake_bot.delete_message = AsyncMock(side_effect=bot.NetworkError("offline"))

        async def stop_after_burst(delay):
            if delay == 60:
                raise asyncio.CancelledError

        with patch.object(bot.asyncio, "sleep", side_effect=stop_after_burst):
            with self.assertRaises(asyncio.CancelledError):
                await bot.delete_transient_messages(self.fake_bot, 7, (401,), 0)
        self.assertEqual(self.fake_bot.delete_message.await_count, 3)
        with closing(bot.history_connection()) as connection:
            self.assertGreater(connection.execute(
                "SELECT due_at FROM transient_message_cleanup WHERE chat_id=7 AND message_id=401"
            ).fetchone()[0], bot.time.time())

    async def test_restart_recovers_delayed_reply_and_active_zip_status(self):
        bot.queue_transient_cleanup(7, (301,), 0)
        bot.queue_transient_cleanup(7, (401,), 0, active=True)
        bot.resume_transient_cleanup(self.fake_bot)
        await asyncio.sleep(0.01)
        self.assertEqual(set(self.fake_bot.deleted), {301, 401})
        with closing(bot.history_connection()) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM transient_message_cleanup"
            ).fetchone()[0], 0)

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
                bot=self.fake_bot, application=FakeApplication(), user_data={}
            )
            await bot.retry_download(SimpleNamespace(callback_query=retry_query), restarted_context)
            await asyncio.gather(*restarted_context.application.tasks)

        self.assertEqual(len(self.fake_bot.sent), 2)
        self.assertIsNone(bot.load_retry_batch(job_id))
        self.assertIn(88, self.fake_bot.deleted)
        self.assertIn(99, self.fake_bot.deleted)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sent_tracks").fetchone()[0], 2)

    async def test_five_downloads_use_at_most_two_slots(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def download(url, media_type, directory, quality):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.08)
                path = directory / "song.mp3"
                path.write_bytes(b"audio")
                return path, "Artist", url[-11:], None
            finally:
                with lock:
                    active -= 1

        async def send_one(index):
            query = FakeQuery()
            query.message.chat_id = index + 100
            url = f"https://www.youtube.com/watch?v=video{index:06d}"
            await bot.deliver_media(query, self.context, [url], "mp3", index + 100, 88)

        with patch.object(bot, "download_media", side_effect=download):
            await asyncio.gather(*(send_one(index) for index in range(5)))

        self.assertEqual(peak, 2)
        self.assertEqual(len(self.fake_bot.sent), 5)

    async def test_slow_upload_keeps_at_most_two_temporary_files(self):
        files = []
        release_uploads = asyncio.Event()
        two_uploads_started = asyncio.Event()
        uploads_started = 0

        def download(url, media_type, directory, quality):
            path = directory / "song.mp3"
            path.write_bytes(b"audio")
            files.append(path)
            return path, "Artist", url[-11:], None

        async def send_audio(**kwargs):
            nonlocal uploads_started
            uploads_started += 1
            if uploads_started == 2:
                two_uploads_started.set()
            await release_uploads.wait()
            return SimpleNamespace(message_id=100 + uploads_started, audio=SimpleNamespace(file_id="file"))

        async def send_one(index):
            query = FakeQuery()
            query.message.chat_id = index + 100
            url = f"https://www.youtube.com/watch?v=video{index:06d}"
            await bot.deliver_media(query, self.context, [url], "mp3", index + 100, 88)

        with patch.object(bot, "download_media", side_effect=download), patch.object(
            self.fake_bot, "send_audio", side_effect=send_audio
        ):
            tasks = [asyncio.create_task(send_one(index)) for index in range(5)]
            try:
                await asyncio.wait_for(two_uploads_started.wait(), timeout=2)
                await asyncio.sleep(0.05)
                self.assertEqual(len(files), 2)
                self.assertTrue(all(path.exists() for path in files))
            finally:
                release_uploads.set()
                await asyncio.gather(*tasks)
        self.assertEqual(len(files), 5)
        self.assertTrue(all(not path.exists() for path in files))

    async def test_five_queued_jobs_leave_cancel_handler_responsive(self):
        slots = asyncio.Semaphore(2)
        await slots.acquire()
        await slots.acquire()
        self.context.application.bot_data["download_slots"] = slots
        job_ids = [f"{index:012d}" for index in range(5)]
        self.context.user_data["jobs"] = {
            job_id: ([f"https://www.youtube.com/watch?v=video{index:06d}"], index + 100, 88)
            for index, job_id in enumerate(job_ids)
        }

        def download(url, media_type, directory, quality):
            path = directory / "song.mp3"
            path.write_bytes(b"audio")
            return path, "Artist", url[-11:], None

        with patch.object(bot, "download_media", side_effect=download) as downloader:
            chooser_updates = []
            for index, job_id in enumerate(job_ids):
                query = FakeQuery(f"quality:{job_id}:mp3:192")
                query.message.chat_id = index + 100
                chooser_updates.append(SimpleNamespace(callback_query=query))
            await asyncio.wait_for(asyncio.gather(*(
                bot.send_media(chooser_update, self.context) for chooser_update in chooser_updates
            )), timeout=1)
            self.assertEqual(len(self.context.application.tasks), 5)
            cancelled_job = job_ids[0]
            await asyncio.wait_for(bot.cancel_download(
                SimpleNamespace(callback_query=FakeQuery(f"cancel:{cancelled_job}")), self.context
            ), timeout=1)
            self.assertTrue(self.context.application.bot_data["active_batches"][cancelled_job]["cancelled"])
            slots.release()
            slots.release()
            await asyncio.wait_for(asyncio.gather(*self.context.application.tasks), timeout=3)

        self.assertEqual(downloader.call_count, 4)
        self.assertEqual(len(self.fake_bot.sent), 4)

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
                "Hozier", f"Song {number}", self.db, file_id=f"saved-{number}",
            )
        results = []
        async def reply_text(text, **kwargs):
            results.append(text)
            return SimpleNamespace(message_id=56)
        message = SimpleNamespace(message_id=55, reply_text=reply_text)
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=7), effective_user=SimpleNamespace(id=42), effective_message=message)
        self.context.args = ["Hozier"]
        await bot.search_command(update, self.context)
        self.assertIn("→ NoName", results[0])
        self.assertEqual(len(self.fake_bot.deleted), 1)

    async def test_search_typo_finds_saved_mp3_and_prioritizes_exact_without_false_matches(self):
        entries = [
            ("Arctic Monkeys", "Do I Wanna Know", "audio-arctic-1"),
            ("Arctic Monkeys", "R U Mine", "audio-arctic-2"),
            ("Artic Monkeys", "Exact Spelling", "audio-exact"),
            ("Article Music", "Monkey Business", "audio-other"),
        ]
        for index, (artist, title, file_id) in enumerate(entries):
            bot.record_sent_track(
                7, f"https://www.youtube.com/watch?v={index:011d}", artist, title,
                file_id=file_id,
            )
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=00000000004", "Arctic Monkeys", "Video Only",
            file_id="video", media_type="mp4",
        )
        update, message = self.private_command_update(chat_id=7)
        message.message_id = 55
        self.context.args = ["artic", "monkeys"]
        await bot.search_command(update, self.context)
        text = message.reply_text.call_args.args[0]
        self.assertIn("Найдено: 4", text)
        self.assertIn("Arctic Monkeys", text)
        self.assertNotIn("Article Music", text)
        self.assertIn("Video Only (MP4)", text)
        buttons = message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        self.assertIn("Exact Spelling", buttons[0][0].text)
        self.assertFalse(any("Video Only" in row[0].text for row in buttons))
        self.assertEqual(self.fake_bot.deleted, [55])

    async def test_search_mp4_only_track_is_text_result_without_audio_button(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=00000000001", "Arctic Monkeys", "Live Show",
            file_id="saved-video", media_type="mp4",
        )
        update, message = self.private_command_update(chat_id=7)
        message.message_id = 55
        self.context.args = ["arctic", "monkeys"]
        await bot.search_command(update, self.context)
        text = message.reply_text.call_args.args[0]
        self.assertIn("Arctic Monkeys — Live Show (MP4) → NoName", text)
        buttons = message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(len(buttons), 1)
        self.assertIn("Закрыть", buttons[0][0].text)
        state = self.context.user_data["saved_search_ui"][(7, None)]
        forged = FakeQuery(f"searchsong:{state['job_id']}:0")
        forged.from_user.id = 7
        forged.message.message_id = 300
        await bot.search_song_action(SimpleNamespace(callback_query=forged), self.context)
        self.assertIn("нет сохранённого MP3", forged.answers[-1][0][0])
        self.assertEqual(self.fake_bot.sent, [])

    async def test_search_reuses_ui_expires_and_saved_audio_double_click_sends_once_in_topic(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=00000000001", "Arctic Monkeys", "Do I Wanna Know",
            file_id="saved-audio",
        )
        first, first_message = self.private_command_update(chat_id=7)
        first_message.message_id = 55
        first_message.message_thread_id = 77
        self.context.args = ["arctic"]
        with patch.object(bot, "SEARCH_UI_TIMEOUT", 0.03):
            await bot.search_command(first, self.context)
            second, second_message = self.private_command_update(chat_id=7)
            second_message.message_id = 56
            second_message.message_thread_id = 77
            self.context.args = ["artic", "monkeys"]
            await bot.search_command(second, self.context)
            self.assertEqual(second_message.reply_text.await_count, 0)
            self.assertEqual(self.fake_bot.edits[-1]["message_id"], 300)
            self.assertEqual(self.fake_bot.deleted, [55, 56])
            state = self.context.user_data["saved_search_ui"][(7, 77)]
            started = asyncio.Event()
            release = asyncio.Event()
            original_send = self.fake_bot.send_audio

            async def slow_send(**kwargs):
                started.set()
                await release.wait()
                return await original_send(**kwargs)

            self.fake_bot.send_audio = slow_send
            data = f"searchsong:{state['job_id']}:0"
            first_click = FakeQuery(data)
            first_click.from_user.id = 7
            first_click.message.message_id = 300
            first_click.message.message_thread_id = 77
            task = asyncio.create_task(bot.search_song_action(SimpleNamespace(callback_query=first_click), self.context))
            await asyncio.wait_for(started.wait(), 1)
            second_click = FakeQuery(data)
            second_click.from_user.id = 7
            second_click.message.message_id = 300
            second_click.message.message_thread_id = 77
            await bot.search_song_action(SimpleNamespace(callback_query=second_click), self.context)
            self.assertIn("уже отправляется", second_click.answers[-1][0][0])
            release.set()
            await task
            self.assertEqual(len(self.fake_bot.sent), 1)
            self.assertEqual(self.fake_bot.sent[0]["audio"], "saved-audio")
            self.assertEqual(self.fake_bot.sent[0]["message_thread_id"], 77)
            self.assertIn(300, self.fake_bot.deleted)
            self.assertNotIn((7, 77), self.context.user_data["saved_search_ui"])
            with closing(sqlite3.connect(self.db)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sent_tracks WHERE chat_id=7").fetchone()[0], 1)

            third, third_message = self.private_command_update(chat_id=7)
            third_message.message_id = 57
            third_message.message_thread_id = 77
            self.context.args = ["arctic"]
            await bot.search_command(third, self.context)
            await asyncio.sleep(0.05)
            self.assertIn(300, self.fake_bot.deleted)
            self.assertNotIn((7, 77), self.context.user_data["saved_search_ui"])

    async def test_new_search_is_not_deleted_by_previous_search_audio_completion(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=00000000001", "Arctic Monkeys", "Song",
            file_id="saved-audio",
        )
        first, first_message = self.private_command_update(chat_id=7)
        first_message.message_id = 55
        self.context.args = ["arctic"]
        await bot.search_command(first, self.context)
        old_job = self.context.user_data["saved_search_ui"][(7, None)]["job_id"]
        started = asyncio.Event()
        release = asyncio.Event()
        original_send = self.fake_bot.send_audio

        async def held_send(**kwargs):
            started.set()
            await release.wait()
            return await original_send(**kwargs)

        self.fake_bot.send_audio = held_send
        query = FakeQuery(f"searchsong:{old_job}:0")
        query.from_user.id = 7
        query.message.message_id = 300
        old_send = asyncio.create_task(bot.search_song_action(SimpleNamespace(callback_query=query), self.context))
        await asyncio.wait_for(started.wait(), 1)
        second, second_message = self.private_command_update(chat_id=7)
        second_message.message_id = 56
        self.context.args = ["song"]
        await bot.search_command(second, self.context)
        new_job = self.context.user_data["saved_search_ui"][(7, None)]["job_id"]
        self.assertNotEqual(new_job, old_job)
        release.set()
        await old_send
        self.assertEqual(self.context.user_data["saved_search_ui"][(7, None)]["job_id"], new_job)
        self.assertNotIn(300, self.fake_bot.deleted)
        old_close = FakeQuery(f"searchclose:7:{old_job}")
        old_close.from_user.id = 7
        old_close.message.message_id = 300
        await bot.close_search(SimpleNamespace(callback_query=old_close), self.context)
        self.assertIn("уже заменён", old_close.answers[-1][0][0])
        self.assertEqual(self.context.user_data["saved_search_ui"][(7, None)]["job_id"], new_job)
        self.assertNotIn(300, self.fake_bot.deleted)

    async def test_search_audio_failure_keeps_retry_button_and_timer(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=00000000001", "Arctic Monkeys", "Song",
            file_id="saved-audio",
        )
        update, message = self.private_command_update(chat_id=7)
        message.message_id = 55
        self.context.args = ["arctic"]
        await bot.search_command(update, self.context)
        state = self.context.user_data["saved_search_ui"][(7, None)]
        query = FakeQuery(f"searchsong:{state['job_id']}:0")
        query.from_user.id = 7
        query.message.message_id = 300
        original_send = self.fake_bot.send_audio
        self.fake_bot.send_audio = AsyncMock(side_effect=bot.TelegramError("temporary"))
        await bot.search_song_action(SimpleNamespace(callback_query=query), self.context)
        self.assertIn("Нажмите песню ещё раз", query.edits[-1][0])
        self.assertEqual(query.edits[-1][1].inline_keyboard[0][0].callback_data, query.data)
        self.assertFalse(state["sending"])
        self.assertFalse(state["timer"].cancelled())
        self.assertEqual(self.fake_bot.deleted, [55])
        self.fake_bot.send_audio = original_send
        await bot.search_song_action(SimpleNamespace(callback_query=query), self.context)
        self.assertEqual(len(self.fake_bot.sent), 1)
        self.assertIn(300, self.fake_bot.deleted)

    async def test_search_answer_failure_does_not_lock_retry_button(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=00000000001", "Arctic Monkeys", "Song",
            file_id="saved-audio",
        )
        update, message = self.private_command_update(chat_id=7)
        message.message_id = 55
        self.context.args = ["arctic"]
        await bot.search_command(update, self.context)
        state = self.context.user_data["saved_search_ui"][(7, None)]
        query = FakeQuery(f"searchsong:{state['job_id']}:0")
        query.from_user.id = 7
        query.message.message_id = 300
        query.answer = AsyncMock(side_effect=bot.TelegramError("answer failed"))
        await bot.search_song_action(SimpleNamespace(callback_query=query), self.context)
        self.assertFalse(state["sending"])
        self.assertFalse(state["timer"].cancelled())
        self.assertEqual(self.fake_bot.sent, [])
        retry = FakeQuery(query.data)
        retry.from_user.id = 7
        retry.message.message_id = 300
        await bot.search_song_action(SimpleNamespace(callback_query=retry), self.context)
        self.assertEqual(len(self.fake_bot.sent), 1)

    async def test_old_failed_audio_send_cannot_overwrite_new_search_ui(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=00000000001", "Arctic Monkeys", "Song",
            file_id="saved-audio",
        )
        first, first_message = self.private_command_update(chat_id=7)
        first_message.message_id = 55
        self.context.args = ["arctic"]
        await bot.search_command(first, self.context)
        old_job = self.context.user_data["saved_search_ui"][(7, None)]["job_id"]
        started = asyncio.Event()
        release = asyncio.Event()

        async def failed_send(**kwargs):
            started.set()
            await release.wait()
            raise bot.TelegramError("send failed")

        self.fake_bot.send_audio = failed_send
        query = FakeQuery(f"searchsong:{old_job}:0")
        query.from_user.id = 7
        query.message.message_id = 300
        old_send = asyncio.create_task(bot.search_song_action(SimpleNamespace(callback_query=query), self.context))
        await asyncio.wait_for(started.wait(), 1)
        second, second_message = self.private_command_update(chat_id=7)
        second_message.message_id = 56
        self.context.args = ["song"]
        await bot.search_command(second, self.context)
        new_state = self.context.user_data["saved_search_ui"][(7, None)]
        release.set()
        await old_send
        self.assertNotEqual(new_state["job_id"], old_job)
        self.assertIs(self.context.user_data["saved_search_ui"][(7, None)], new_state)
        self.assertEqual(query.edits, [])
        self.assertNotIn(300, self.fake_bot.deleted)

    async def test_failed_search_ui_replacement_preserves_old_expiry(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=00000000001", "Arctic Monkeys", "Song",
            file_id="saved-audio",
        )
        first, first_message = self.private_command_update(chat_id=7)
        first_message.message_id = 55
        self.context.args = ["arctic"]
        with patch.object(bot, "SEARCH_UI_TIMEOUT", 0.02):
            await bot.search_command(first, self.context)
            old = self.context.user_data["saved_search_ui"][(7, None)]
            second, second_message = self.private_command_update(chat_id=7)
            second_message.message_id = 56
            second_message.reply_text.side_effect = bot.TelegramError("reply failed")
            with patch.object(self.fake_bot, "edit_message_text", side_effect=bot.TelegramError("edit failed")):
                with self.assertRaises(bot.TelegramError):
                    await bot.search_command(second, self.context)
            self.assertIs(self.context.user_data["saved_search_ui"][(7, None)], old)
            self.assertFalse(old["timer"].cancelled())
            await asyncio.sleep(0.04)
        self.assertIn(300, self.fake_bot.deleted)
        self.assertNotIn((7, None), self.context.user_data["saved_search_ui"])

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
        bot.record_sent_track(7, url, "Hozier", "Too Sweet", self.db, file_id="saved-audio", message_id=123)
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
        self.assertEqual(self.fake_bot.topics, [{"chat_id": 7, "name": "Избранные"}])
        self.assertEqual(self.fake_bot.copies[0]["message_id"], 123)
        self.assertFalse(self.fake_bot.sent)
        self.assertTrue(deleted)
        message.text = "/unfavorite"
        await bot.change_favorite(SimpleNamespace(effective_message=message), self.context)
        self.assertEqual(bot.favorite_tracks(7, self.db), [])
        self.assertIn(601, self.fake_bot.deleted)

    async def test_heart_reaction_creates_one_copy_and_removes_it(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=video000023", "Miyagi", "Captain",
            self.db, file_id="saved-audio", message_id=123, message_thread_id=400,
        )
        def reaction(actor, old, new, message_id=123):
            return bot.types.UpdateBotMessageReaction(
                peer=bot.types.PeerUser(7), msg_id=message_id, date=None,
                actor=bot.types.PeerUser(actor), old_reactions=old,
                new_reactions=new, qts=1,
            )
        heart = bot.types.ReactionEmoji("❤")  # Telegram omits the variation selector.
        await bot.handle_reaction_event(self.context.application, reaction(7, [], [heart]))
        await bot.handle_reaction_event(self.context.application, reaction(7, [], [heart]))
        self.assertEqual(len(self.fake_bot.copies), 1)
        self.assertEqual(self.fake_bot.copies[0]["message_thread_id"], 501)
        self.assertEqual(bot.favorite_tracks(7, self.db), [("Miyagi", "Captain")])
        await bot.handle_reaction_event(self.context.application, reaction(8, [heart], []))
        await bot.handle_reaction_event(self.context.application, reaction(7, [], [bot.types.ReactionEmoji("👍")]))
        self.assertEqual(bot.favorite_tracks(7, self.db), [("Miyagi", "Captain")])
        await bot.handle_reaction_event(self.context.application, reaction(7, [heart], []))
        self.assertEqual(bot.favorite_tracks(7, self.db), [])
        self.assertEqual(self.fake_bot.deleted, [601])
        with closing(bot.history_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM favorite_copies").fetchone()[0], 0)
        await bot.handle_reaction_event(self.context.application, reaction(7, [], [heart], 999))
        self.assertEqual(len(self.fake_bot.copies), 1)

    async def test_heart_reaction_accepts_uncatalogued_bot_audio(self):
        client = SimpleNamespace(
            get_messages=AsyncMock(return_value=SimpleNamespace(
                audio=SimpleNamespace(),
                file=SimpleNamespace(performer="Ирина Кайратовна, Shiza", title="Kõk tu"),
                reply_to=SimpleNamespace(reply_to_top_id=400),
            )),
            delete_messages=AsyncMock(),
        )
        self.context.application.bot_data["reaction_client"] = client
        heart = bot.types.ReactionEmoji("❤")
        def event(old, new):
            return bot.types.UpdateBotMessageReaction(
                peer=bot.types.PeerUser(7), msg_id=847, date=None,
                actor=bot.types.PeerUser(7), old_reactions=old,
                new_reactions=new, qts=1,
            )
        await bot.handle_reaction_event(self.context.application, event([], [heart]))
        self.assertEqual(self.fake_bot.copies[0]["message_id"], 847)
        self.assertEqual(bot.favorite_tracks(7, self.db), [("Ирина Кайратовна, Shiza", "Kõk tu")])
        await bot.handle_reaction_event(self.context.application, event([heart], []))
        client.delete_messages.assert_awaited_once_with(7, [601], revoke=True)
        self.assertEqual(bot.favorite_tracks(7, self.db), [])

    async def test_promoted_song_keeps_favorite_copy_mapping(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=video000028", "Miyagi", "Captain",
            self.db, file_id="saved-audio", message_id=123, message_thread_id=400,
        )
        await bot.sync_favorite_copy(self.context.application, 7, 123, True)
        await bot.move_recent_misc_tracks(self.context, 7, "Miyagi", 400, 502)
        with closing(bot.history_connection()) as connection:
            self.assertEqual(
                connection.execute("SELECT message_id FROM sent_tracks WHERE chat_id=7").fetchone()[0], 602
            )
            self.assertEqual(
                connection.execute("SELECT source_message_id FROM favorite_copies WHERE chat_id=7").fetchone()[0], 602
            )
        await bot.sync_favorite_copy(self.context.application, 7, 602, False)
        self.assertEqual(bot.favorite_tracks(7, self.db), [])
        self.assertIn(601, self.fake_bot.deleted)

    async def test_favorite_removal_updates_flag_and_delete_intent_atomically(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=video000029", "Drake", "Headlines",
            self.db, file_id="saved-audio", message_id=123,
        )
        await bot.sync_favorite_copy(self.context.application, 7, 123, True)
        with closing(bot.history_connection()) as connection:
            with connection:
                connection.execute(
                    "CREATE TRIGGER reject_favorite_delete BEFORE UPDATE OF pending_delete ON favorite_copies "
                    "WHEN NEW.pending_delete=1 BEGIN SELECT RAISE(ABORT, 'test interruption'); END"
                )
        with self.assertRaises(sqlite3.IntegrityError):
            await bot.sync_favorite_copy(self.context.application, 7, 123, False)
        with closing(bot.history_connection()) as connection:
            self.assertEqual(connection.execute("SELECT favorite FROM sent_tracks").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT pending_delete FROM favorite_copies").fetchone()[0], 0)
            with connection:
                connection.execute("DROP TRIGGER reject_favorite_delete")
        await bot.sync_favorite_copy(self.context.application, 7, 123, False)
        self.assertEqual(bot.favorite_tracks(7, self.db), [])

    async def test_existing_favorite_is_restored_after_restart(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=video000024", "Drake", "One Dance",
            self.db, file_id="saved-audio", message_id=234,
        )
        bot.set_favorite(7, 234, True, self.db)
        await bot.backfill_favorite_topic(self.context.application)
        await bot.backfill_favorite_topic(self.context.application)
        self.assertEqual(len(self.fake_bot.copies), 1)
        with closing(bot.history_connection()) as connection:
            self.assertEqual(connection.execute("SELECT favorite_message_id FROM favorite_copies").fetchone()[0], 601)

    async def test_favorite_copy_retries_after_temporary_copy_failure(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=video000025", "The Weeknd", "Starboy",
            self.db, file_id="saved-audio", message_id=235,
        )
        original_copy = self.fake_bot.copy_message
        self.fake_bot.copy_message = AsyncMock(side_effect=bot.NetworkError("temporary"))
        self.assertTrue(await bot.sync_favorite_copy(self.context.application, 7, 235, True))
        self.assertEqual(bot.favorite_tracks(7, self.db), [("The Weeknd", "Starboy")])
        with closing(bot.history_connection()) as connection:
            self.assertEqual(connection.execute("SELECT favorite_message_id FROM favorite_copies").fetchone()[0], 0)
        self.fake_bot.copy_message = original_copy
        await bot.backfill_favorite_topic(self.context.application)
        self.assertEqual(len(self.fake_bot.copies), 1)

    async def test_pending_favorite_deletion_recovers(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=video000026", "Drake", "Headlines",
            self.db, file_id="saved-audio", message_id=236,
        )
        await bot.sync_favorite_copy(self.context.application, 7, 236, True)
        original_delete = self.fake_bot.delete_message
        self.fake_bot.delete_message = AsyncMock(side_effect=bot.NetworkError("temporary"))
        with patch.object(bot.asyncio, "sleep", new=AsyncMock()):
            await bot.sync_favorite_copy(self.context.application, 7, 236, False)
        with closing(bot.history_connection()) as connection:
            self.assertEqual(connection.execute("SELECT pending_delete FROM favorite_copies").fetchone()[0], 1)
        self.fake_bot.delete_message = original_delete
        await bot.retry_pending_favorite_deletions(self.context.application)
        with closing(bot.history_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM favorite_copies").fetchone()[0], 0)
        self.assertEqual(self.fake_bot.deleted, [601])

    async def test_favorite_removal_uses_bot_mtproto_session(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=video000027", "Tame Impala", "Nangs",
            self.db, file_id="saved-audio", message_id=237,
        )
        await bot.sync_favorite_copy(self.context.application, 7, 237, True)
        client = SimpleNamespace(delete_messages=AsyncMock())
        self.context.application.bot_data["reaction_client"] = client
        await bot.sync_favorite_copy(self.context.application, 7, 237, False)
        client.delete_messages.assert_awaited_once_with(7, [601], revoke=True)
        self.assertEqual(self.fake_bot.deleted, [])
        with closing(bot.history_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM favorite_copies").fetchone()[0], 0)

    async def test_reaction_listener_uses_separate_bot_session(self):
        fake_client = SimpleNamespace(
            add_event_handler=unittest.mock.Mock(), start=AsyncMock(), disconnect=AsyncMock()
        )
        settings = {
            "TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "a" * 32,
            "TELEGRAM_BOT_TOKEN": "bot-token", "TELEGA_BOT_SESSION_PATH": "/tmp/test-bot-reactions",
        }
        with patch.dict(bot.os.environ, settings), patch.object(bot, "TelegramClient", return_value=fake_client) as client:
            await bot.start_reaction_listener(self.context.application)
        client.assert_called_once_with("/tmp/test-bot-reactions", 12345, "a" * 32, catch_up=True)
        fake_client.start.assert_awaited_once_with(bot_token="bot-token")
        self.assertIs(self.context.application.bot_data["reaction_client"], fake_client)
        await bot.stop_reaction_listener(self.context.application)
        fake_client.disconnect.assert_awaited_once()

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

    async def test_mix_link_shows_full_labels_and_keeps_page_numbers(self):
        labels = [
            f"Исполнитель с длинным именем {number} — Полное название композиции и приглашённые исполнители"
            for number in range(9)
        ]
        entries = [(f"https://www.youtube.com/watch?v=video{n:06d}", label, "")
                   for n, label in enumerate(labels)]
        edits = []

        async def edit_text(text, **kwargs):
            self.assertTrue(kwargs.get("disable_web_page_preview"))
            edits.append((text, kwargs["reply_markup"]))

        message = SimpleNamespace(
            chat_id=7, message_id=88, text="https://www.youtube.com/playlist?list=RDvideo000001",
            reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=edit_text)),
        )
        with patch.object(bot, "list_playlist", return_value=("Mix", entries, False)):
            await bot.receive_url(SimpleNamespace(effective_message=message), self.context)
        for index, label in enumerate(labels[:8], 1):
            self.assertIn(f"{index}. {label}", edits[-1][0])
        self.assertNotIn(labels[8], edits[-1][0])
        self.assertIn("по номеру", edits[-1][0])
        self.assertEqual(edits[-1][1].inline_keyboard[0][0].text, "⬜ Выбрать №1")
        job_id = next(iter(self.context.user_data["mixes"]))
        toggle = FakeQuery(f"mix:{job_id}:toggle:0")
        await bot.select_playlist(SimpleNamespace(callback_query=toggle), self.context)
        self.assertIn(labels[0], toggle.edits[-1][0])
        self.assertTrue(toggle.edit_kwargs[-1]["disable_web_page_preview"])
        self.assertEqual(toggle.edits[-1][1].inline_keyboard[0][0].text, "☑️ Выбран №1")
        page = FakeQuery(f"mix:{job_id}:page:1")
        await bot.select_playlist(SimpleNamespace(callback_query=page), self.context)
        self.assertIn(f"9. {labels[8]}", page.edits[-1][0])
        self.assertNotIn(labels[0], page.edits[-1][0])
        self.assertTrue(page.edit_kwargs[-1]["disable_web_page_preview"])
        self.assertEqual(page.edits[-1][1].inline_keyboard[0][0].text, "⬜ Выбрать №9")
        self.assertEqual(page.edits[-1][1].inline_keyboard[0][0].callback_data,
                         f"mix:{job_id}:toggle:8")

    def test_track_lists_stay_within_telegram_limit_for_long_unicode_labels(self):
        entries = [(index, "🎵" * 5000) for index in range(10)]
        text = bot.track_list_text("Результаты", entries)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4000)
        for index in range(1, 11):
            self.assertIn(f"\n\n{index}. ", text)
        self.assertEqual(text.count("…"), 10)

    def test_playlist_and_search_preserve_full_labels_and_separate_artist(self):
        title = "Полное название композиции " * 8
        artist = "Полное имя исполнителя " * 4
        entry = {"id": "video000071", "title": title, "artist": artist, "duration": 169}

        class FakeYoutubeDL:
            def __init__(self, settings):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def extract_info(self, url, download):
                return {"_type": "playlist", "title": "Mix", "entries": [entry]}

        expected = f"{artist.strip()} - {title.strip()}"
        with patch.object(bot, "YoutubeDL", FakeYoutubeDL):
            self.assertEqual(bot.list_playlist("https://www.youtube.com/playlist?list=PLtest")[1][0][1], expected)
            self.assertEqual(bot.search_youtube("запрос")[0][1], expected)
        self.assertEqual(bot.track_display_label({"artist": "Artist", "title": "Artist - Song"}),
                         "Artist - Song")
        for artist, title in (("M", "Money"), ("Muse", "Museum")):
            with self.subTest(artist=artist):
                self.assertEqual(bot.track_display_label({"artist": artist, "title": title}),
                                 f"{artist} - {title}")

    async def test_mix_remember_preserves_artist_and_title_from_producer_metadata(self):
        class FakeYoutubeDL:
            def __init__(self, settings):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def extract_info(self, url, download):
                return {"_type": "playlist", "title": "Mix", "entries": [
                    {"id": "video000071", "artist": "Adele", "title": "Hello"},
                ]}

        status = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(
            chat_id=7, message_id=88, text="https://www.youtube.com/playlist?list=PLtest",
            reply_text=AsyncMock(return_value=status),
        )
        with patch.object(bot, "YoutubeDL", FakeYoutubeDL):
            await bot.receive_url(SimpleNamespace(effective_message=message), self.context)
        job_id = next(iter(self.context.user_data["mixes"]))
        self.assertIn("1. Adele - Hello", status.edit_text.await_args.args[0])
        await bot.select_playlist(SimpleNamespace(callback_query=FakeQuery(f"mix:{job_id}:toggle:0")),
                                  self.context)
        await bot.select_playlist(SimpleNamespace(callback_query=FakeQuery(f"mix:{job_id}:remember:now")),
                                  self.context)
        with closing(bot.history_connection()) as connection:
            row = connection.execute(
                "SELECT artist, title, song_key FROM sent_tracks WHERE chat_id=? AND video_id=?",
                (7, "video000071"),
            ).fetchone()
        self.assertEqual(row, ("Adele", "Hello", "adele|hello"))
        self.assertNotIn(job_id, self.context.user_data["mixes"])

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

    async def test_alias_and_featured_artists_use_largest_catalog_group(self):
        for number, artist in enumerate(("Gruppa Skryptonite", "Скриптонит", "Скриптонит", "Сёстры")):
            bot.record_sent_track(7, f"https://www.youtube.com/watch?v={number:011d}", artist,
                                  f"Solo {number}", self.db)
        bot.record_sent_track(7, "https://www.youtube.com/watch?v=00000000004",
                              "Сёстры", "Высота (feat. Скриптонит)", self.db)
        groups = dict(bot.library_groups(7, self.db))
        self.assertEqual(set(groups), {"Скриптонит", "Сёстры"})
        self.assertEqual(len(groups["Скриптонит"]), 4)
        self.assertTrue(any(track["artist"] == "Сёстры" for track in groups["Скриптонит"]))
        self.assertEqual(bot.artist_track_count(7, "Скриптонит"), 4)

    async def test_collaboration_majority_and_tie_prefers_lead(self):
        for number in range(4):
            bot.record_sent_track(7, f"https://www.youtube.com/watch?v={number:011d}",
                                  "madk1d", f"Solo {number}", self.db)
        bot.record_sent_track(7, "https://www.youtube.com/watch?v=00000000004",
                              "Егор Крид", "MALO 2.0 (ft. OG Buda, Toxi$, madk1d)", self.db)
        bot.record_sent_track(7, "https://www.youtube.com/watch?v=00000000005",
                              "VILLIAN, madk1d", "ДИНАСТИЯ", self.db)
        groups = dict(bot.library_groups(7, self.db))
        self.assertEqual(len(groups["madk1d"]), 6)
        self.assertTrue(any(track["artist"] == "VILLIAN, madk1d" for track in groups["madk1d"]))
        self.assertEqual(bot.assigned_artist(7, "VILLIAN, madk1d", "ДИНАСТИЯ"), "madk1d")
        self.assertEqual(bot.artist_track_count(7, "madk1d"), 6)
        self.assertEqual(bot.primary_artist("A, B"), "A")
        self.assertEqual(bot.catalog_artist("A, B", "Song", {"a": 2, "b": 2}), "A")

    async def test_video_title_features_survive_clean_track_metadata(self):
        artist, title = bot.music_metadata({
            "title": "Egor Krid - MALO 2.0 (ft. OG Buda, Toxi$, madk1d)",
            "artist": "Егор Крид", "track": "MALO 2.0",
        })
        self.assertEqual(artist, "Егор Крид")
        self.assertIn("madk1d", title)
        artist, title = bot.music_metadata({"title": "Высота (feat. Скриптонит) - Сёстры"})
        self.assertEqual((artist, title), ("Сёстры", "Высота (feat. Скриптонит)"))
        artist, title = bot.music_metadata({
            "title": "Высота (feat. Скриптонит) - Сёстры", "artist": "Сёстры",
        })
        self.assertEqual((artist, title), ("Сёстры", "Высота (feat. Скриптонит)"))
        artist, title = bot.music_metadata({
            "title": "Высота (feat. Скриптонит) - Сёстры", "track": "Высота",
        })
        self.assertEqual((artist, title), ("Сёстры", "Высота (feat. Скриптонит)"))
        self.assertEqual(bot.music_metadata({"title": "Artist - Song"}), ("Artist", "Song"))
        artist, title = bot.music_metadata({
            "title": "Egor Krid - MALO 2.0 (ft. OG Buda, Toxi$, madk1d)",
            "artist": "Егор Крид", "track": "MALO 2.0 (ft. OG Buda)",
        })
        self.assertIn("madk1d", title)

    async def test_search_uses_canonical_artist_name(self):
        bot.record_sent_track(7, "https://www.youtube.com/watch?v=00000000001",
                              "Gruppa Skryptonite", "Положение", self.db, file_id="saved")
        update, message = self.private_command_update()
        message.message_id = 55
        self.context.args = ["Скриптонит"]
        await bot.search_command(update, self.context)
        self.assertIn("Найдено: 1", message.reply_text.call_args.args[0])

    async def test_existing_alias_topic_is_reused(self):
        with closing(bot.history_connection(self.db)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO artist_topics (chat_id, artist_key, message_thread_id) VALUES (7, ?, 99)",
                    ("gruppa skryptonite",),
                )
        self.assertEqual(await bot.artist_topic(self.context, 7, "Скриптонит"), 99)
        self.assertEqual(self.fake_bot.topics, [])
        self.assertEqual(await bot.artist_topic(self.context, 7, "Gruppa Skryptonite"), 99)

    async def test_new_featured_track_creates_popular_artist_topic(self):
        for number in range(4):
            bot.record_sent_track(7, f"https://www.youtube.com/watch?v={number:011d}",
                                  "madk1d", f"Solo {number}", self.db)
        self.fake_bot.topics_enabled = True
        url = "https://www.youtube.com/watch?v=00000000004"

        def download(_url, _media_type, directory, _quality):
            path = directory / "song.mp3"
            path.write_bytes(b"audio")
            return path, "Егор Крид", "MALO 2.0 (ft. OG Buda, madk1d)", None

        with patch.object(bot, "download_media", side_effect=download):
            await bot.deliver_media(FakeQuery(), self.context, [url], "mp3", 7, 88)
        self.assertEqual([topic["name"] for topic in self.fake_bot.topics], ["madk1d"])
        self.assertEqual(self.fake_bot.sent[-1]["message_thread_id"], 501)
        self.assertEqual(self.fake_bot.sent[-1]["performer"], "Егор Крид")
        self.assertEqual(bot.artist_track_count(7, "madk1d"), 5)

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
            await asyncio.gather(*self.context.application.tasks)
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
            await asyncio.gather(*self.context.application.tasks)
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
            await asyncio.gather(*self.context.application.tasks)
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
        label = "Jace June — Come Home (полное название песни с длинным подзаголовком и гостями)"
        results = [(
            "https://www.youtube.com/watch?v=video000072",
            label, bot.song_key("Jace June", "Come Home"), 169,
        )]
        edits = []
        async def edit_text(text, **kwargs):
            self.assertTrue(kwargs.get("disable_web_page_preview"))
            edits.append((text, kwargs.get("reply_markup")))
        async def reply_text(text, **kwargs):
            return SimpleNamespace(message_id=99, edit_text=edit_text)
        message = SimpleNamespace(chat_id=7, message_id=88, reply_text=reply_text)
        update = SimpleNamespace(
            effective_message=message, effective_user=SimpleNamespace(id=42),
        )
        self.context.args = ["Jace", "June", "Come", "Home"]
        with patch.object(bot, "search_youtube", return_value=results):
            await bot.findout_command(update, self.context)
        self.assertIn("[2:49]", edits[-1][1].inline_keyboard[0][0].text)
        self.assertIn(f"1. {label}", edits[-1][0])
        self.assertIn("по номеру", edits[-1][0])
        self.assertEqual(edits[-1][1].inline_keyboard[0][0].text, "⬜ Выбрать №1 [2:49]")
        job_id = next(iter(self.context.user_data["youtube_searches"]))
        pick = FakeQuery(f"find:{job_id}:0")
        await bot.findout_action(SimpleNamespace(callback_query=pick), self.context)
        self.assertIn("выбрано 1", pick.edits[-1][0])
        self.assertIn(f"1. {label}", pick.edits[-1][0])
        self.assertTrue(pick.edit_kwargs[-1]["disable_web_page_preview"])
        self.assertEqual(pick.edits[-1][1].inline_keyboard[0][0].text, "☑️ Выбран №1 [2:49]")
        download_selected = FakeQuery(f"find:{job_id}:download")
        await bot.findout_action(SimpleNamespace(callback_query=download_selected), self.context)
        self.assertIn("Теперь выберите формат", download_selected.edits[-1][0])
        choose_format = FakeQuery(f"mp3:{job_id}")
        await bot.send_media(SimpleNamespace(callback_query=choose_format), self.context)
        choose_quality = FakeQuery(f"quality:{job_id}:mp3:192")
        with patch.object(bot, "deliver_media", new_callable=AsyncMock) as deliver:
            await bot.send_media(SimpleNamespace(callback_query=choose_quality), self.context)
            await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(deliver.await_args.args[2], [results[0][0]])
        self.assertEqual(deliver.await_args.args[5], 88)
        self.assertEqual(deliver.await_args.kwargs["quality"], "192")

    async def test_findout_format_and_quality_can_be_closed_without_download(self):
        for open_quality in (False, True):
            with self.subTest(open_quality=open_quality):
                job_id = "abcdef123456"
                timer = asyncio.create_task(asyncio.sleep(60))
                self.context.user_data["jobs"] = {job_id: (
                    ["https://www.youtube.com/watch?v=video000072"], 7, 88, 42, {}, None,
                )}
                self.context.user_data["findout_format_timers"] = {job_id: {
                    "chat_id": 7, "thread_id": None, "message_id": 99, "task": timer,
                }}
                if open_quality:
                    choose = FakeQuery(f"mp3:{job_id}")
                    await bot.send_media(SimpleNamespace(callback_query=choose), self.context)
                    markup = choose.edits[-1][1]
                else:
                    markup = bot.format_markup(job_id, dismiss=True)
                self.assertEqual(markup.inline_keyboard[-1][0].callback_data, f"format:{job_id}:close")
                outsider = FakeQuery(f"format:{job_id}:close")
                outsider.from_user.id = 43
                await bot.send_media(SimpleNamespace(callback_query=outsider), self.context)
                self.assertIn("чужой", outsider.answers[-1][0][0])
                self.assertIn(job_id, self.context.user_data["jobs"])
                close = FakeQuery(f"format:{job_id}:close")
                await bot.send_media(SimpleNamespace(callback_query=close), self.context)
                self.assertIn(99, self.fake_bot.deleted)
                self.assertNotIn(job_id, self.context.user_data["jobs"])
                self.assertNotIn(job_id, self.context.user_data["findout_format_timers"])
                await asyncio.sleep(0)
                self.assertTrue(timer.cancelled())
                stale = FakeQuery(f"mp3:{job_id}")
                await bot.send_media(SimpleNamespace(callback_query=stale), self.context)
                self.assertIn("устарела", stale.answers[-1][0][0])

    async def test_findout_quality_hides_choice_buttons_and_cleans_panel(self):
        job_id = "abcdef123456"
        url = "https://www.youtube.com/watch?v=video000072"
        bot.record_sent_track(7, url, "Artist", "Song", file_id="already")
        timer = asyncio.create_task(asyncio.sleep(60))
        self.context.user_data["jobs"] = {job_id: ([url], 7, 88, 42, {}, None)}
        self.context.user_data["findout_format_timers"] = {job_id: {
            "chat_id": 7, "thread_id": None, "message_id": 99, "task": timer,
        }}
        choose = FakeQuery(f"quality:{job_id}:mp3:192")
        await bot.send_media(SimpleNamespace(callback_query=choose), self.context)
        self.assertEqual(choose.edits[0][0], "Начинаю загрузку…")
        self.assertEqual(choose.edits[0][1].inline_keyboard[0][0].callback_data, f"cancel:{job_id}")
        await asyncio.sleep(0)
        self.assertTrue(timer.cancelled())
        await asyncio.gather(*self.context.application.tasks)
        self.assertIn(99, self.fake_bot.deleted)
        self.assertNotIn(job_id, self.context.application.bot_data["active_batches"])

    async def test_old_format_panel_is_removed_after_restart_but_active_download_is_kept(self):
        job_id = "abcdef123456"
        stale = FakeQuery(f"mp3:{job_id}")
        await bot.send_media(SimpleNamespace(callback_query=stale), self.context)
        self.assertIn(99, self.fake_bot.deleted)
        self.assertIn("устарела", stale.answers[-1][0][0])
        self.context.application.bot_data["active_batches"] = {job_id: {"cancelled": False}}
        active = FakeQuery(f"mp3:{job_id}")
        await bot.send_media(SimpleNamespace(callback_query=active), self.context)
        self.assertEqual(self.fake_bot.deleted.count(99), 1)
        self.assertIn("уже идёт", active.answers[-1][0][0])

    async def test_stale_quality_button_preserves_failed_download_retry_panel(self):
        job_id = "abcdef123456"
        bot.save_retry_batch(
            job_id, 42, 7, 88, "mp3", ["https://youtu.be/abc"], {}, quality="192",
        )
        stale = FakeQuery(f"quality:{job_id}:mp3:192")
        await bot.send_media(SimpleNamespace(callback_query=stale), self.context)
        self.assertEqual(self.fake_bot.deleted, [])
        self.assertIn("кнопку повтора", stale.answers[-1][0][0])
        self.assertIsNotNone(bot.load_retry_batch(job_id))

    async def test_findout_format_expiry_waits_for_quality_selection(self):
        job_id = "abcdef123456"
        self.context.user_data["jobs"] = {job_id: (["https://youtu.be/abc"], 7, 88, 42, {}, None)}
        choose = FakeQuery(f"quality:{job_id}:mp3:192")

        async def slow_edit(*args, **kwargs):
            await asyncio.sleep(0.03)

        choose.edit_message_text = slow_edit
        with patch.object(bot, "SEARCH_UI_TIMEOUT", 0.01), patch.object(
            bot, "deliver_media", new_callable=AsyncMock,
        ) as deliver:
            timer = asyncio.create_task(bot.expire_findout_format(self.context, job_id))
            self.context.user_data["findout_format_timers"] = {job_id: {
                "chat_id": 7, "thread_id": None, "message_id": 99, "task": timer,
            }}
            await bot.send_media(SimpleNamespace(callback_query=choose), self.context)
            await asyncio.gather(*self.context.application.tasks)
            await asyncio.sleep(0)
        self.assertTrue(timer.cancelled())
        self.assertEqual(self.fake_bot.deleted, [])
        self.assertNotIn(job_id, self.context.user_data["findout_format_timers"])
        self.assertNotIn(job_id, self.context.user_data["jobs"])
        deliver.assert_awaited_once()

    async def test_findout_results_expiry_waits_for_format_selection(self):
        job_id = "abcdef123456"
        url = "https://www.youtube.com/watch?v=video000072"
        job = {
            "entries": [(url, "Artist - Song", bot.song_key("Artist", "Song"), 120)],
            "selected": {0}, "user_id": 42, "chat_id": 7, "thread_id": None,
            "source_message_id": 88, "source_deleted": True,
            "status_message_id": 99, "timer": None,
        }
        self.context.user_data["youtube_searches"] = {job_id: job}
        self.context.user_data["findout_current"] = {(7, None): job_id}
        download = FakeQuery(f"find:{job_id}:download")

        async def slow_edit(*args, **kwargs):
            await asyncio.sleep(0.03)

        download.edit_message_text = slow_edit
        with patch.object(bot, "SEARCH_UI_TIMEOUT", 0.01):
            timer = asyncio.create_task(bot.expire_findout(self.context, job_id))
            job["timer"] = timer
            await bot.findout_action(SimpleNamespace(callback_query=download), self.context)
            await asyncio.sleep(0)
        self.assertTrue(timer.cancelled())
        self.assertEqual(self.fake_bot.deleted, [])
        self.assertNotIn(job_id, self.context.user_data["youtube_searches"])
        self.assertIn(job_id, self.context.user_data["jobs"])
        format_timer = self.context.user_data["findout_format_timers"].pop(job_id)["task"]
        format_timer.cancel()

    async def test_findout_quality_edit_failure_keeps_choice_available(self):
        job_id = "abcdef123456"
        timer = asyncio.create_task(asyncio.sleep(60))
        self.context.user_data["jobs"] = {job_id: (["https://youtu.be/abc"], 7, 88, 42, {}, None)}
        self.context.user_data["findout_format_timers"] = {job_id: {
            "chat_id": 7, "thread_id": None, "message_id": 99, "task": timer,
        }}
        choose = FakeQuery(f"quality:{job_id}:mp3:192")
        choose.edit_message_text = AsyncMock(side_effect=bot.TelegramError("edit failed"))
        await bot.send_media(SimpleNamespace(callback_query=choose), self.context)
        self.assertIn("Повторите кнопку", choose.answers[-1][0][0])
        self.assertIn(job_id, self.context.user_data["jobs"])
        self.assertIn(job_id, self.context.user_data["findout_format_timers"])
        self.assertEqual(self.context.application.tasks, [])
        timer.cancel()

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

    async def test_findout_multi_select_blocks_saved_and_preserves_order_quality_and_cancel(self):
        results = [
            (f"https://www.youtube.com/watch?v={number:011d}", f"Artist - Song {number}",
             bot.song_key("Artist", f"Song {number}"), 120 + number)
            for number in range(3)
        ]
        bot.record_sent_track(7, results[1][0], "Artist", "Song 1", file_id="already")
        status_edits = []

        async def edit_text(text, **kwargs):
            status_edits.append((text, kwargs.get("reply_markup")))

        async def reply_text(text, **kwargs):
            return SimpleNamespace(message_id=99, edit_text=edit_text)

        message = SimpleNamespace(chat_id=7, message_id=88, message_thread_id=77, reply_text=reply_text)
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=42))
        self.context.args = ["Artist", "Song"]
        with patch.object(bot, "search_youtube", return_value=results), patch.object(bot, "SEARCH_UI_TIMEOUT", 0.2):
            await bot.findout_command(update, self.context)
            self.assertEqual(self.fake_bot.deleted, [88])
            self.assertEqual(status_edits[-1][1].inline_keyboard[1][0].text, "✅ Уже есть №2 [2:01]")
            job_id = next(iter(self.context.user_data["youtube_searches"]))

            def choice(value, user_id=42, thread=77):
                query = FakeQuery(f"find:{job_id}:{value}")
                query.from_user.id = user_id
                query.message.message_thread_id = thread
                return query

            saved = choice(1)
            await bot.findout_action(SimpleNamespace(callback_query=saved), self.context)
            self.assertIn("уже есть", saved.answers[-1][0][0])
            outsider = choice(0, user_id=43)
            await bot.findout_action(SimpleNamespace(callback_query=outsider), self.context)
            self.assertIn("чужой", outsider.answers[-1][0][0])
            wrong_thread = choice(0, thread=78)
            await bot.findout_action(SimpleNamespace(callback_query=wrong_thread), self.context)
            self.assertIn("прежней теме", wrong_thread.answers[-1][0][0])
            for index in (2, 0):
                await bot.findout_action(SimpleNamespace(callback_query=choice(index)), self.context)
            self.assertEqual(self.context.user_data["youtube_searches"][job_id]["selected"], {0, 2})
            download = choice("download")
            await bot.findout_action(SimpleNamespace(callback_query=download), self.context)
            self.assertIn("Выбрано записей: 2", download.edits[-1][0])
            self.assertEqual(self.context.user_data["jobs"][job_id][0], [results[0][0], results[2][0]])
            await asyncio.sleep(0.04)
            self.assertNotIn(99, self.fake_bot.deleted)  # active format choice resets its own timer
            format_wrong_thread = FakeQuery(f"mp3:{job_id}")
            format_wrong_thread.message.message_thread_id = 78
            await bot.send_media(SimpleNamespace(callback_query=format_wrong_thread), self.context)
            self.assertIn("прежней теме", format_wrong_thread.answers[-1][0][0])
            choose_format = FakeQuery(f"mp3:{job_id}")
            choose_format.message.message_thread_id = 77
            await bot.send_media(SimpleNamespace(callback_query=choose_format), self.context)
            choose_quality = FakeQuery(f"quality:{job_id}:mp3:192")
            choose_quality.message.message_thread_id = 77
            release = asyncio.Event()

            async def held_delivery(*args, **kwargs):
                await release.wait()

            with patch.object(bot, "deliver_media", side_effect=held_delivery) as deliver:
                await bot.send_media(SimpleNamespace(callback_query=choose_quality), self.context)
                self.assertEqual(deliver.call_args.args[2], [results[0][0], results[2][0]])
                self.assertEqual(deliver.call_args.args[6], {
                    results[0][0]: results[0][2], results[2][0]: results[2][2],
                })
                self.assertEqual(deliver.call_args.kwargs["quality"], "192")
                cancel = FakeQuery(f"cancel:{job_id}")
                await bot.cancel_download(SimpleNamespace(callback_query=cancel), self.context)
                self.assertTrue(self.context.application.bot_data["active_batches"][job_id]["cancelled"])
                release.set()
                await asyncio.gather(*self.context.application.tasks)
            await asyncio.sleep(0.22)
            self.assertNotIn(99, self.fake_bot.deleted)  # timer cannot erase active download UI

    async def test_abandoned_findout_format_screen_expires_and_cannot_download(self):
        entry = (
            "https://www.youtube.com/watch?v=00000000001", "Artist - New Song",
            bot.song_key("Artist", "New Song"), 120,
        )

        async def reply_text(text, **kwargs):
            return SimpleNamespace(message_id=99, edit_text=AsyncMock())

        message = SimpleNamespace(chat_id=7, message_id=88, reply_text=reply_text)
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=42))
        self.context.args = ["Artist", "New", "Song"]
        with patch.object(bot, "search_youtube", return_value=[entry]), patch.object(bot, "SEARCH_UI_TIMEOUT", 0.02):
            await bot.findout_command(update, self.context)
            job_id = next(iter(self.context.user_data["youtube_searches"]))
            await bot.findout_action(
                SimpleNamespace(callback_query=FakeQuery(f"find:{job_id}:0")), self.context,
            )
            await bot.findout_action(
                SimpleNamespace(callback_query=FakeQuery(f"find:{job_id}:download")), self.context,
            )
            await asyncio.sleep(0.04)
        self.assertIn(99, self.fake_bot.deleted)
        self.assertNotIn(job_id, self.context.user_data["jobs"])
        old_format = FakeQuery(f"mp3:{job_id}")
        await bot.send_media(SimpleNamespace(callback_query=old_format), self.context)
        self.assertIn("устарела", old_format.answers[-1][0][0])

    async def test_findout_rechecks_saved_at_confirmation_and_removes_abandoned_results(self):
        results = [
            (f"https://www.youtube.com/watch?v={number:011d}", f"Artist - Song {number}",
             bot.song_key("Artist", f"Song {number}"), 100)
            for number in range(2)
        ]
        async def reply_text(text, **kwargs):
            return SimpleNamespace(message_id=99, edit_text=AsyncMock())
        message = SimpleNamespace(chat_id=7, message_id=88, reply_text=reply_text)
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=42))
        self.context.args = ["Artist", "Song"]
        with patch.object(bot, "search_youtube", return_value=results), patch.object(bot, "SEARCH_UI_TIMEOUT", 0.02):
            await bot.findout_command(update, self.context)
            job_id = next(iter(self.context.user_data["youtube_searches"]))
            for index in (0, 1):
                await bot.findout_action(
                    SimpleNamespace(callback_query=FakeQuery(f"find:{job_id}:{index}")), self.context,
                )
            bot.record_sent_track(7, results[0][0], "Artist", "Song 0", file_id="already")
            download = FakeQuery(f"find:{job_id}:download")
            await bot.findout_action(SimpleNamespace(callback_query=download), self.context)
            self.assertIn("пропущено: 1", download.edits[-1][0])
            self.assertEqual(self.context.user_data["jobs"][job_id][0], [results[1][0]])
            self.context.user_data["findout_format_timers"][job_id]["task"].cancel()
            self.context.user_data["findout_format_timers"].pop(job_id)
            self.context.user_data["jobs"].pop(job_id)
            await bot.findout_command(update, self.context)
            second_job_id = next(iter(self.context.user_data["youtube_searches"]))
            await asyncio.sleep(0.04)
        self.assertIn(99, self.fake_bot.deleted)
        self.assertNotIn(second_job_id, self.context.user_data["youtube_searches"])

    async def test_findout_empty_result_ui_is_removed_after_inactivity(self):
        async def reply_text(text, **kwargs):
            return SimpleNamespace(message_id=99, edit_text=AsyncMock())
        message = SimpleNamespace(chat_id=7, message_id=88, reply_text=reply_text)
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=42))
        self.context.args = ["unknown", "song"]
        with patch.object(bot, "search_youtube", return_value=[]), patch.object(bot, "SEARCH_UI_TIMEOUT", 0.02):
            await bot.findout_command(update, self.context)
            await asyncio.sleep(0.04)
        self.assertEqual(self.fake_bot.deleted, [88, 99])
        self.assertEqual(self.context.user_data["findout_empty_ui"], {})

    async def test_findout_format_edit_failure_preserves_selection_and_cleanup(self):
        entry = (
            "https://www.youtube.com/watch?v=00000000001", "Artist - Song",
            bot.song_key("Artist", "Song"), 100,
        )

        async def reply_text(text, **kwargs):
            return SimpleNamespace(message_id=99, edit_text=AsyncMock())

        message = SimpleNamespace(chat_id=7, message_id=88, reply_text=reply_text)
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=42))
        self.context.args = ["Artist", "Song"]
        with patch.object(bot, "search_youtube", return_value=[entry]), patch.object(bot, "SEARCH_UI_TIMEOUT", 0.02):
            await bot.findout_command(update, self.context)
            job_id = next(iter(self.context.user_data["youtube_searches"]))
            await bot.findout_action(
                SimpleNamespace(callback_query=FakeQuery(f"find:{job_id}:0")), self.context,
            )
            download = FakeQuery(f"find:{job_id}:download")
            download.edit_message_text = AsyncMock(side_effect=bot.TelegramError("edit failed"))
            await bot.findout_action(SimpleNamespace(callback_query=download), self.context)
            self.assertIn("Повторите кнопку", download.answers[-1][0][0])
            self.assertIn(job_id, self.context.user_data["youtube_searches"])
            self.assertNotIn(job_id, self.context.user_data.get("jobs", {}))
            self.assertFalse(self.context.user_data["youtube_searches"][job_id]["timer"].cancelled())
            await asyncio.sleep(0.04)
        self.assertIn(99, self.fake_bot.deleted)
        self.assertNotIn(job_id, self.context.user_data["youtube_searches"])

    async def test_findout_results_edit_failure_cleans_untracked_status(self):
        entry = (
            "https://www.youtube.com/watch?v=00000000001", "Artist - Song",
            bot.song_key("Artist", "Song"), 100,
        )
        replies = []

        async def reply_text(text, **kwargs):
            replies.append(text)
            if len(replies) == 1:
                return SimpleNamespace(message_id=99, edit_text=AsyncMock(side_effect=bot.TelegramError("edit failed")))
            return SimpleNamespace(message_id=100, edit_text=AsyncMock())

        message = SimpleNamespace(chat_id=7, message_id=88, reply_text=reply_text)
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=42))
        self.context.args = ["Artist", "Song"]
        with patch.object(bot, "search_youtube", return_value=[entry]), patch.object(bot, "SEARCH_UI_TIMEOUT", 0.02):
            await bot.findout_command(update, self.context)
            self.assertEqual(self.context.user_data["youtube_searches"], {})
            self.assertEqual(self.context.user_data["findout_current"], {})
            self.assertIn(99, self.fake_bot.deleted)
            self.assertIn("Не удалось показать", replies[-1])
            await asyncio.sleep(0.04)
        self.assertIn(100, self.fake_bot.deleted)

    async def test_search_and_findout_replace_each_other_in_same_chat_topic(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=00000000001", "Arctic Monkeys", "Song",
            file_id="saved-audio",
        )
        search, search_message = self.private_command_update(chat_id=7)
        search_message.message_id = 55
        search_message.message_thread_id = 77
        self.context.args = ["arctic"]
        await bot.search_command(search, self.context)
        self.assertIn((7, 77), self.context.user_data["saved_search_ui"])

        async def find_reply(text, **kwargs):
            return SimpleNamespace(message_id=500, edit_text=AsyncMock())

        find_message = SimpleNamespace(chat_id=7, message_id=88, message_thread_id=77, reply_text=find_reply)
        find = SimpleNamespace(effective_message=find_message, effective_user=SimpleNamespace(id=7))
        self.context.args = ["Arctic", "Song"]
        entry = (
            "https://www.youtube.com/watch?v=00000000002", "Arctic - New Song",
            bot.song_key("Arctic", "New Song"), 100,
        )
        with patch.object(bot, "search_youtube", return_value=[entry]):
            await bot.findout_command(find, self.context)
        self.assertIn(300, self.fake_bot.deleted)
        self.assertNotIn((7, 77), self.context.user_data["saved_search_ui"])
        job_id = next(iter(self.context.user_data["youtube_searches"]))
        self.assertEqual(self.context.user_data["findout_current"][(7, 77)], job_id)

        latest, latest_message = self.private_command_update(chat_id=7)
        latest_message.message_id = 56
        latest_message.message_thread_id = 77
        self.context.args = ["song"]
        await bot.search_command(latest, self.context)
        self.assertIn(500, self.fake_bot.deleted)
        self.assertNotIn(job_id, self.context.user_data["youtube_searches"])
        self.assertNotIn((7, 77), self.context.user_data["findout_current"])
        self.assertIn((7, 77), self.context.user_data["saved_search_ui"])

    async def test_share_link_is_private_random_and_survives_restart(self):
        owner, owner_message = self.private_command_update()
        self.context.args = []
        await bot.share_command(owner, self.context)
        first_text = owner_message.reply_text.call_args.args[0]
        token = first_text.split("start=share_", 1)[1].splitlines()[0]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{32}$")
        self.assertLessEqual(len("share_" + token), 64)
        self.assertIn("Любой человек", first_text)
        await bot.share_command(owner, self.context)
        self.assertIn("start=share_" + token, owner_message.reply_text.call_args.args[0])

        other_owner, other_message = self.private_command_update(chat_id=9)
        await bot.share_command(other_owner, self.context)
        other_token = other_message.reply_text.call_args.args[0].split("start=share_", 1)[1].splitlines()[0]
        self.assertNotEqual(token, other_token)
        self.assertEqual(bot.share_owner(token), 7)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute(
                "SELECT owner_chat_id FROM shared_playlists WHERE token = ?", (token,)
            ).fetchone()[0], 7)

        restarted_context = SimpleNamespace(
            bot=FakeBot(), args=["share_" + token], application=FakeApplication()
        )
        viewer, viewer_message = self.private_command_update(chat_id=8)
        await bot.start(viewer, restarted_context)
        self.assertIn("пока нет доступных MP3", viewer_message.reply_text.call_args.args[0])
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        restarted_context.args = ["share_" + tampered]
        await bot.start(viewer, restarted_context)
        self.assertIn("недействительна", viewer_message.reply_text.call_args.args[0])

        group, group_message = self.private_command_update(chat_id=-7, chat_type="supergroup")
        await bot.share_command(group, self.context)
        self.assertIn("только в личном чате", group_message.reply_text.call_args.args[0])
        self.assertIsNone(bot.share_owner("A" * 32))

    async def test_shared_audio_uses_file_id_without_download_or_history_write(self):
        url = "https://www.youtube.com/watch?v=video000081"
        bot.record_sent_track(7, url, "Artist", "Song", self.db, file_id="saved-audio-id")
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=video000082", "Artist", "MP4 only", self.db,
            media_type="mp4", file_id="video-id",
        )
        token = bot.get_or_create_share_token(7)
        viewer, viewer_message = self.private_command_update(chat_id=8)
        self.context.args = ["share_" + token]
        await bot.start(viewer, self.context)
        artist_button = viewer_message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        artist_query = FakeQuery(artist_button.callback_data)
        artist_query.message.chat_id = 8
        await bot.shared_playlist_action(SimpleNamespace(callback_query=artist_query), self.context)
        track_buttons = artist_query.edits[-1][1].inline_keyboard
        self.assertEqual(len(track_buttons), 2)  # one song and the back button
        self.assertIn("Song", track_buttons[0][0].text)
        track_query = FakeQuery(track_buttons[0][0].callback_data)
        track_query.message.chat_id = 8
        with patch.object(bot, "download_media") as downloader:
            await bot.shared_playlist_action(SimpleNamespace(callback_query=track_query), self.context)
            track_query.message.message_thread_id = 77
            await bot.shared_playlist_action(SimpleNamespace(callback_query=track_query), self.context)
        downloader.assert_not_called()
        self.assertEqual(len(self.fake_bot.sent), 2)
        self.assertEqual(self.fake_bot.sent[0]["audio"], "saved-audio-id")
        self.assertEqual(self.fake_bot.sent[0]["chat_id"], 8)
        self.assertNotIn("message_thread_id", self.fake_bot.sent[0])
        self.assertEqual(self.fake_bot.sent[1]["message_thread_id"], 77)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM sent_tracks WHERE chat_id = 8"
            ).fetchone()[0], 0)

    async def test_share_pagination_and_revocation_reject_old_buttons(self):
        for number in range(17):
            artist = "Artist 00" if number < 9 else f"Artist {number - 8:02d}"
            bot.record_sent_track(
                7, f"https://www.youtube.com/watch?v=video{number:06d}",
                artist, f"Song {number:02d}", self.db, file_id=f"audio-{number}",
            )
        token = bot.get_or_create_share_token(7)
        viewer, viewer_message = self.private_command_update(chat_id=8)
        self.context.args = ["share_" + token]
        await bot.start(viewer, self.context)
        artist_buttons = viewer_message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(len(artist_buttons), 10)  # eight artists, pagination, ZIP
        next_artists = FakeQuery(artist_buttons[-2][0].callback_data)
        next_artists.message.chat_id = 8
        await bot.shared_playlist_action(SimpleNamespace(callback_query=next_artists), self.context)
        self.assertIn("Artist 08", next_artists.edits[-1][1].inline_keyboard[0][0].text)

        first_artist = FakeQuery(artist_buttons[0][0].callback_data)
        first_artist.message.chat_id = 8
        await bot.shared_playlist_action(SimpleNamespace(callback_query=first_artist), self.context)
        track_buttons = first_artist.edits[-1][1].inline_keyboard
        self.assertEqual(len(track_buttons), 10)  # eight songs, pagination, back
        next_tracks = FakeQuery(track_buttons[-2][0].callback_data)
        next_tracks.message.chat_id = 8
        await bot.shared_playlist_action(SimpleNamespace(callback_query=next_tracks), self.context)
        self.assertIn("Song 08", next_tracks.edits[-1][1].inline_keyboard[0][0].text)

        owner, owner_message = self.private_command_update()
        await bot.unshare_command(owner, self.context)
        self.assertIn("Доступ", owner_message.reply_text.call_args.args[0])
        old_track = FakeQuery(track_buttons[0][0].callback_data)
        old_track.message.chat_id = 8
        await bot.shared_playlist_action(SimpleNamespace(callback_query=old_track), self.context)
        self.assertTrue(old_track.answers[-1][1]["show_alert"])
        self.assertFalse(self.fake_bot.sent)
        await bot.start(viewer, self.context)
        self.assertIn("отозвана", viewer_message.reply_text.call_args.args[0])
        self.assertIsNone(bot.share_owner(token))

        replacement = bot.get_or_create_share_token(7)
        self.assertNotEqual(replacement, token)
        self.assertIsNone(bot.share_owner(token))

    async def test_unshare_waits_for_in_flight_send_before_revoking(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=video000083", "Artist", "Song",
            self.db, file_id="saved-audio-id",
        )
        token = bot.get_or_create_share_token(7)
        query = FakeQuery(f"shr:{token}:s:video000083")
        query.message.chat_id = 8
        answer_started = asyncio.Event()
        resume_answer = asyncio.Event()

        async def paused_answer(*args, **kwargs):
            answer_started.set()
            await resume_answer.wait()

        owner, _ = self.private_command_update()
        with patch.object(query, "answer", side_effect=paused_answer):
            send_task = asyncio.create_task(bot.shared_playlist_action(
                SimpleNamespace(callback_query=query), self.context
            ))
            await asyncio.wait_for(answer_started.wait(), timeout=1)
            revoke_task = asyncio.create_task(bot.unshare_command(owner, self.context))
            try:
                await asyncio.sleep(0.02)
                self.assertFalse(revoke_task.done())
            finally:
                resume_answer.set()
                await asyncio.gather(send_task, revoke_task)

        self.assertEqual(len(self.fake_bot.sent), 1)
        self.assertIsNone(bot.share_owner(token))
        old_query = FakeQuery(f"shr:{token}:s:video000083")
        old_query.message.chat_id = 8
        await bot.shared_playlist_action(SimpleNamespace(callback_query=old_query), self.context)
        self.assertEqual(len(self.fake_bot.sent), 1)
        self.assertTrue(old_query.answers[-1][1]["show_alert"])

    async def test_unshare_waits_for_share_link_reply(self):
        owner, owner_message = self.private_command_update()
        reply_started = asyncio.Event()
        resume_reply = asyncio.Event()
        order = []
        token = None

        async def paused_share_reply(text, **kwargs):
            nonlocal token
            token = text.split("start=share_", 1)[1].splitlines()[0]
            reply_started.set()
            await resume_reply.wait()
            order.append("share")

        async def unshare_reply(text, **kwargs):
            order.append("unshare")

        owner_message.reply_text = AsyncMock(side_effect=paused_share_reply)
        unshare_update, unshare_message = self.private_command_update()
        unshare_message.reply_text = AsyncMock(side_effect=unshare_reply)
        share_task = asyncio.create_task(bot.share_command(owner, self.context))
        await asyncio.wait_for(reply_started.wait(), timeout=1)
        self.assertEqual(bot.share_owner(token), 7)
        revoke_task = asyncio.create_task(bot.unshare_command(unshare_update, self.context))
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(revoke_task.done())
            self.assertEqual(bot.share_owner(token), 7)
        finally:
            resume_reply.set()
            await asyncio.gather(share_task, revoke_task)
        self.assertEqual(order, ["share", "unshare"])
        self.assertIsNone(bot.share_owner(token))

    async def test_unshare_waits_for_share_username_lookup(self):
        with closing(bot.history_connection()):
            pass
        owner, owner_message = self.private_command_update()
        unshare_update, unshare_message = self.private_command_update()
        lookup_started = asyncio.Event()
        resume_lookup = asyncio.Event()
        order = []

        async def paused_get_me():
            lookup_started.set()
            await resume_lookup.wait()
            return SimpleNamespace(username="musica_test_bot")

        async def share_reply(text, **kwargs):
            order.append("share")

        async def unshare_reply(text, **kwargs):
            order.append("unshare")

        owner_message.reply_text = AsyncMock(side_effect=share_reply)
        unshare_message.reply_text = AsyncMock(side_effect=unshare_reply)
        with patch.object(self.fake_bot, "get_me", side_effect=paused_get_me):
            share_task = asyncio.create_task(bot.share_command(owner, self.context))
            await asyncio.wait_for(lookup_started.wait(), timeout=1)
            revoke_task = asyncio.create_task(bot.unshare_command(unshare_update, self.context))
            try:
                await asyncio.sleep(0.02)
                self.assertFalse(revoke_task.done())
                with closing(sqlite3.connect(self.db)) as connection:
                    self.assertIsNone(connection.execute(
                        "SELECT token FROM shared_playlists WHERE owner_chat_id = 7"
                    ).fetchone())
            finally:
                resume_lookup.set()
                await asyncio.gather(share_task, revoke_task)
        self.assertEqual(order, ["share", "unshare"])
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT token FROM shared_playlists WHERE owner_chat_id = 7"
            ).fetchone())

    async def test_unshare_waits_for_catalog_reply_and_navigation_edit(self):
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=video000084", "Artist", "Song",
            self.db, file_id="saved-audio-id",
        )
        token = bot.get_or_create_share_token(7)
        viewer, viewer_message = self.private_command_update(chat_id=8)
        reply_started = asyncio.Event()
        resume_reply = asyncio.Event()
        order = []

        async def paused_catalog_reply(text, **kwargs):
            reply_started.set()
            await resume_reply.wait()
            order.append("catalog")

        async def unshare_reply(text, **kwargs):
            order.append("unshare")

        viewer_message.reply_text = AsyncMock(side_effect=paused_catalog_reply)
        unshare_update, unshare_message = self.private_command_update()
        unshare_message.reply_text = AsyncMock(side_effect=unshare_reply)
        self.context.args = ["share_" + token]
        open_task = asyncio.create_task(bot.start(viewer, self.context))
        await asyncio.wait_for(reply_started.wait(), timeout=1)
        revoke_task = asyncio.create_task(bot.unshare_command(unshare_update, self.context))
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(revoke_task.done())
        finally:
            resume_reply.set()
            await asyncio.gather(open_task, revoke_task)
        self.assertEqual(order, ["catalog", "unshare"])

        replacement = bot.get_or_create_share_token(7)
        query = FakeQuery(f"shr:{replacement}:a:0")
        query.message.chat_id = 8
        edit_started = asyncio.Event()
        resume_edit = asyncio.Event()
        order.clear()

        async def paused_edit(text, **kwargs):
            edit_started.set()
            await resume_edit.wait()
            order.append("edit")

        query.edit_message_text = AsyncMock(side_effect=paused_edit)
        edit_task = asyncio.create_task(bot.shared_playlist_action(
            SimpleNamespace(callback_query=query), self.context
        ))
        await asyncio.wait_for(edit_started.wait(), timeout=1)
        revoke_task = asyncio.create_task(bot.unshare_command(unshare_update, self.context))
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(revoke_task.done())
        finally:
            resume_edit.set()
            await asyncio.gather(edit_task, revoke_task)
        self.assertEqual(order, ["edit", "unshare"])
        self.assertIsNone(bot.share_owner(replacement))

    async def test_share_waits_for_unshare_confirmation_reply(self):
        old_token = bot.get_or_create_share_token(7)
        owner, owner_message = self.private_command_update()
        reply_started = asyncio.Event()
        resume_reply = asyncio.Event()
        order = []

        async def paused_unshare_reply(text, **kwargs):
            reply_started.set()
            await resume_reply.wait()
            order.append("unshare")

        async def share_reply(text, **kwargs):
            order.append("share")

        owner_message.reply_text = AsyncMock(side_effect=paused_unshare_reply)
        share_update, share_message = self.private_command_update()
        share_message.reply_text = AsyncMock(side_effect=share_reply)
        revoke_task = asyncio.create_task(bot.unshare_command(owner, self.context))
        await asyncio.wait_for(reply_started.wait(), timeout=1)
        self.assertIsNone(bot.share_owner(old_token))
        share_task = asyncio.create_task(bot.share_command(share_update, self.context))
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(share_task.done())
            with closing(sqlite3.connect(self.db)) as connection:
                self.assertIsNone(connection.execute(
                    "SELECT token FROM shared_playlists WHERE owner_chat_id = 7"
                ).fetchone())
        finally:
            resume_reply.set()
            await asyncio.gather(revoke_task, share_task)
        self.assertEqual(order, ["unshare", "share"])
        self.assertNotEqual(bot.get_or_create_share_token(7), old_token)

    def seed_shared_tracks(self, count=12):
        for number in range(count):
            artist = "Large Artist" if number >= 4 else "Small Artist"
            bot.record_sent_track(
                7, f"https://www.youtube.com/watch?v={number:011d}", artist,
                f"Song {number:03d}", file_id=f"saved-{number}",
            )

    async def open_topics(self, token, chat_id=8, context=None):
        context = context or self.context
        context.args = ["share_" + token]
        update, message = self.private_command_update(chat_id=chat_id)
        await bot.start(update, context)
        await asyncio.gather(*context.application.tasks)
        return message

    async def test_topic_groups_preserve_metadata_and_copy_190_once_across_restart(self):
        self.seed_shared_tracks(190)
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=00000000190", "Video Artist", "Only video",
            file_id="video-only", media_type="mp4",
        )
        token = bot.get_or_create_share_token(7)
        self.fake_bot.topics_enabled = True
        with patch.object(bot, "SHARE_SEND_INTERVAL", 0), patch.object(bot, "download_media", side_effect=AssertionError("download")):
            await self.open_topics(token)
            self.assertEqual({entry["name"] for entry in self.fake_bot.topics}, {"NoName", "Large Artist"})
            self.assertEqual(len(self.fake_bot.sent), 190)
            self.assertTrue(all(entry["chat_id"] == 8 and entry["audio"].startswith("saved-") for entry in self.fake_bot.sent))
            self.assertEqual(self.fake_bot.sent[0]["performer"], "Large Artist")
            self.assertTrue(any(entry["performer"] == "Small Artist" and entry["title"].startswith("Song") for entry in self.fake_bot.sent))
            await self.open_topics(token)
            self.assertEqual(len(self.fake_bot.sent), 190)
            restarted = SimpleNamespace(bot=FakeBot(), application=FakeApplication(), args=[])
            restarted.bot.topics_enabled = True
            restarted.application.bot = restarted.bot
            await self.open_topics(token, context=restarted)
            self.assertEqual(restarted.bot.sent, [])
            self.assertEqual(restarted.bot.topics, [])
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sent_tracks WHERE chat_id=7").fetchone()[0], 191)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shared_recipient_tracks WHERE recipient_chat_id=8").fetchone()[0], 190)

    async def test_five_recipients_get_separate_topics_and_track_ledgers(self):
        self.seed_shared_tracks(6)
        token = bot.get_or_create_share_token(7)
        self.fake_bot.topics_enabled = True
        with patch.object(bot, "SHARE_SEND_INTERVAL", 0):
            self.context.args = ["share_" + token]
            updates = [self.private_command_update(chat_id=chat_id)[0] for chat_id in range(8, 13)]
            await asyncio.gather(*(bot.start(update, self.context) for update in updates))
            await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(len(self.fake_bot.sent), 30)
        self.assertEqual({entry["chat_id"] for entry in self.fake_bot.sent}, set(range(8, 13)))
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shared_recipient_tracks").fetchone()[0], 30)

    async def test_owner_opening_own_link_does_not_duplicate_library(self):
        self.seed_shared_tracks(8)
        token = bot.get_or_create_share_token(7)
        self.fake_bot.topics_enabled = True
        self.context.args = ["share_" + token]
        owner, message = self.private_command_update(chat_id=7)
        await bot.start(owner, self.context)
        self.assertIn("ваша подборка", message.reply_text.call_args.args[0])
        self.assertEqual(self.context.application.tasks, [])
        self.assertEqual(self.fake_bot.topics, [])
        self.assertEqual(self.fake_bot.sent, [])
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shared_recipient_subscriptions").fetchone()[0], 0)

    async def test_pending_sync_resumes_after_restart_without_new_start(self):
        self.seed_shared_tracks(8)
        token = bot.get_or_create_share_token(7)
        bot.save_shared_subscription(7, 8, token, 300)
        fresh = FakeApplication()
        fresh.bot = FakeBot()
        fresh.bot.topics_enabled = True
        with patch.object(bot, "SHARE_SEND_INTERVAL", 0):
            await bot.resume_pending_shared_syncs(fresh)
            await asyncio.gather(*fresh.tasks)
        self.assertEqual(len(fresh.bot.sent), 8)
        self.assertEqual(bot.pending_shared_subscriptions(), [])

    async def test_revoke_stops_topic_sync_after_current_send(self):
        self.seed_shared_tracks(8)
        token = bot.get_or_create_share_token(7)
        self.fake_bot.topics_enabled = True
        started = asyncio.Event()
        release = asyncio.Event()
        original_send = self.fake_bot.send_audio

        async def slow_send(**kwargs):
            started.set()
            await release.wait()
            return await original_send(**kwargs)

        self.fake_bot.send_audio = slow_send
        with patch.object(bot, "SHARE_SEND_INTERVAL", 0):
            self.context.args = ["share_" + token]
            viewer, _ = self.private_command_update(chat_id=8)
            await bot.start(viewer, self.context)
            await asyncio.wait_for(started.wait(), 1)
            owner, _ = self.private_command_update(chat_id=7)
            revoke = asyncio.create_task(bot.unshare_command(owner, self.context))
            await asyncio.sleep(0)
            release.set()
            await revoke
            await asyncio.gather(*self.context.application.tasks)
        self.assertLessEqual(len(self.fake_bot.sent), 1)
        self.assertIsNone(bot.share_owner(token))
        self.assertEqual(bot.pending_shared_subscriptions(), [])

    async def test_partial_failure_retries_only_missing_and_separates_recipients(self):
        self.seed_shared_tracks(8)
        token = bot.get_or_create_share_token(7)
        self.fake_bot.topics_enabled = True
        original_send = self.fake_bot.send_audio
        failed = False

        async def fail_once(**kwargs):
            nonlocal failed
            if kwargs["audio"] == "saved-5" and not failed:
                failed = True
                raise bot.TelegramError("temporary")
            return await original_send(**kwargs)

        self.fake_bot.send_audio = fail_once
        with patch.object(bot, "SHARE_SEND_INTERVAL", 0):
            await self.open_topics(token)
            self.assertEqual(len(self.fake_bot.sent), 7)
            self.assertEqual(len(bot.pending_shared_subscriptions()), 1)
            await self.open_topics(token)
            self.assertEqual(len(self.fake_bot.sent), 8)
            await self.open_topics(token, chat_id=9)
        self.assertEqual(len(self.fake_bot.sent), 16)
        self.assertEqual({entry["chat_id"] for entry in self.fake_bot.sent}, {8, 9})

    async def test_artist_threshold_creates_topic_and_moves_recipient_copies(self):
        self.seed_shared_tracks(4)
        token = bot.get_or_create_share_token(7)
        self.fake_bot.topics_enabled = True
        with patch.object(bot, "SHARE_SEND_INTERVAL", 0):
            await self.open_topics(token)
            self.assertEqual([entry["name"] for entry in self.fake_bot.topics], ["NoName"])
            bot.record_sent_track(7, "https://www.youtube.com/watch?v=00000000004", "Small Artist", "Song 004", file_id="saved-4")
            await self.open_topics(token)
            self.assertIn("Small Artist", [entry["name"] for entry in self.fake_bot.topics])
            self.assertEqual(len(self.fake_bot.sent), 9)
            self.assertEqual(len(self.fake_bot.deleted), 4)
            await self.open_topics(token)
            self.assertEqual(len(self.fake_bot.sent), 9)

    async def test_flood_retry_and_empty_song_key_use_video_id(self):
        bot.record_sent_key(
            7, "https://www.youtube.com/watch?v=00000000001", "",
            artist="Artist", title="Original", audio_file_id="saved",
        )
        token = bot.get_or_create_share_token(7)
        self.fake_bot.topics_enabled = True
        original_send = self.fake_bot.send_audio
        calls = 0

        async def flood_once(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise bot.RetryAfter(0)
            return await original_send(**kwargs)

        self.fake_bot.send_audio = flood_once
        with patch.object(bot, "SHARE_SEND_INTERVAL", 0):
            await self.open_topics(token)
            await self.open_topics(token)
        self.assertEqual(calls, 2)
        self.assertEqual(len(self.fake_bot.sent), 1)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT track_key FROM shared_recipient_tracks").fetchone()[0], "00000000001")

    async def test_deleted_topic_is_recreated_when_next_send_detects_it(self):
        self.seed_shared_tracks(10)
        token = bot.get_or_create_share_token(7)
        self.fake_bot.topics_enabled = True
        with patch.object(bot, "SHARE_SEND_INTERVAL", 0):
            await self.open_topics(token)
            old_large_thread = next(500 + i for i, entry in enumerate(self.fake_bot.topics, 1) if entry["name"] == "Large Artist")
            bot.record_sent_track(
                7, "https://www.youtube.com/watch?v=00000000010", "Large Artist", "Song 010", file_id="saved-10",
            )
            original_send = self.fake_bot.send_audio
            failed = False

            async def stale_once(**kwargs):
                nonlocal failed
                if kwargs["message_thread_id"] == old_large_thread and not failed:
                    failed = True
                    raise bot.BadRequest("Message thread not found")
                return await original_send(**kwargs)

            self.fake_bot.send_audio = stale_once
            await self.open_topics(token)
        self.assertTrue(failed)
        self.assertEqual([entry["name"] for entry in self.fake_bot.topics].count("Large Artist"), 2)
        new_thread = 500 + len(self.fake_bot.topics)
        restored = [entry for entry in self.fake_bot.sent if entry["message_thread_id"] == new_thread]
        self.assertEqual({entry["audio"] for entry in restored}, {f"saved-{number}" for number in range(4, 11)})
        self.assertEqual(len(restored), 7)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shared_recipient_tracks WHERE recipient_chat_id=8").fetchone()[0], 11)

    async def test_zip_button_owner_and_recipient_archives_with_limits(self):
        self.seed_shared_tracks(6)
        bot.record_sent_track(
            7, "https://www.youtube.com/watch?v=00000000000", "Small/Artist", "../Song: 000",
            file_id="saved-0",
        )
        for number in range(6):
            self.fake_bot.files[f"saved-{number}"] = bytes([number + 1]) * 3000
        token = bot.get_or_create_share_token(7)
        owner, owner_message = self.private_command_update(chat_id=7)
        await bot.share_command(owner, self.context)
        owner_markup = owner_message.reply_text.call_args.kwargs["reply_markup"]
        self.assertEqual(owner_markup.inline_keyboard[0][0].callback_data, "zip:" + token)

        self.fake_bot.topics_enabled = True
        self.context.args = ["share_" + token]
        viewer, viewer_message = self.private_command_update(chat_id=8)
        await bot.start(viewer, self.context)
        self.assertEqual(viewer_message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data, "zip:" + token)
        await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(self.fake_bot.edits[-1]["reply_markup"].inline_keyboard[0][0].callback_data, "zip:" + token)

        query = FakeQuery("zip:" + token)
        query.message.chat_id = 8
        with patch.object(bot, "ZIP_PART_TARGET_BYTES", 10_000), patch.object(
            bot, "download_media", side_effect=AssertionError("YouTube download")
        ):
            await bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context)
            await asyncio.gather(*self.context.application.tasks)
        self.assertGreater(len(self.fake_bot.documents), 1)
        names = []
        for document in self.fake_bot.documents:
            self.assertEqual(document["chat_id"], 8)
            self.assertLess(len(document["content"]), 10_000)
            with zipfile.ZipFile(io.BytesIO(document["content"])) as archive:
                self.assertEqual(archive.testzip(), None)
                names.extend(archive.namelist())
        self.assertEqual(len(names), 6)
        self.assertEqual(len(set(names)), 6)
        self.assertTrue(all(name.endswith(".mp3") for name in names))
        self.assertTrue(all("/" not in name and "\\" not in name for name in names))
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sent_tracks WHERE chat_id=7").fetchone()[0], 6)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sent_tracks WHERE chat_id=8").fetchone()[0], 0)

    async def test_zip_sends_largest_artist_first_with_names_and_song_captions(self):
        for number, artist in enumerate(("Small", "Large", "Large", "Large")):
            title = f"Song {number}" if number != 3 else "Very long title " * 100
            bot.record_sent_track(7, f"https://www.youtube.com/watch?v={number:011d}",
                                  artist, title, self.db, file_id=f"saved-{number}")
            self.fake_bot.files[f"saved-{number}"] = b"audio"
        query = FakeQuery("zip:own")
        query.from_user.id = 7
        with patch.object(bot, "ZIP_PART_TARGET_BYTES", 10_000):
            await bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context)
            await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(len(self.fake_bot.documents), 2)
        self.assertTrue(self.fake_bot.documents[0]["filename"].startswith("Large-part-"))
        self.assertTrue(self.fake_bot.documents[1]["filename"].startswith("Small-part-"))
        self.assertIn("Song 1", self.fake_bot.documents[0]["caption"])
        self.assertIn("Song 0", self.fake_bot.documents[1]["caption"])
        self.assertLessEqual(len(self.fake_bot.documents[0]["caption"]), 1024)

    async def test_zip_stop_preserves_sent_part_and_skips_remaining_artist(self):
        for number, artist in enumerate(("Large", "Large", "Small")):
            bot.record_sent_track(7, f"https://www.youtube.com/watch?v={number:011d}",
                                  artist, f"Song {number}", self.db, file_id=f"saved-{number}")
            self.fake_bot.files[f"saved-{number}"] = b"audio"
        token = bot.get_or_create_share_token(7)
        for recipient, data in ((8, "zip:" + token), (7, "zip:own")):
            self.fake_bot.documents.clear()
            entered = asyncio.Event()
            release = asyncio.Event()
            original_send = self.fake_bot.send_document

            async def held_send(**kwargs):
                entered.set()
                await release.wait()
                return await original_send(**kwargs)

            self.fake_bot.send_document = held_send
            query = FakeQuery(data)
            query.message.chat_id = recipient
            query.from_user.id = recipient
            await bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context)
            await asyncio.wait_for(entered.wait(), 1)
            state = self.context.application.bot_data["zip_archive_states"][recipient]
            stop = FakeQuery("zipstop:" + state["job_id"])
            stop.message.chat_id = recipient
            stop.message.message_id = state["status_id"]
            stop.from_user.id = recipient + 1
            await bot.stop_zip_archive(SimpleNamespace(callback_query=stop), self.context)
            self.assertFalse(state["cancelled"])
            stop.from_user.id = recipient
            await bot.stop_zip_archive(SimpleNamespace(callback_query=stop), self.context)
            self.assertTrue(state["cancelled"])
            release.set()
            with patch.object(bot, "ZIP_SUCCESS_STATUS_SECONDS", 0):
                await asyncio.gather(*self.context.application.tasks)
                await asyncio.sleep(0.01)
            self.assertEqual(len(self.fake_bot.documents), 1)
            self.assertIn("ZIP остановлен", self.fake_bot.edits[-1]["text"])
            self.assertIn(state["status_id"], self.fake_bot.deleted)
            self.fake_bot.send_document = original_send

    async def test_zip_stop_while_queued_skips_all_fetches(self):
        bot.record_sent_track(7, "https://www.youtube.com/watch?v=00000000001",
                              "Artist", "Song", self.db, file_id="saved")
        gate = asyncio.Semaphore(0)
        self.context.application.bot_data["zip_archive_gate"] = gate
        query = FakeQuery("zip:own")
        query.from_user.id = 7
        await bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context)
        for _ in range(20):
            if self.context.application.bot_data["zip_archive_states"][7]["status_id"]:
                break
            await asyncio.sleep(0)
        state = self.context.application.bot_data["zip_archive_states"][7]
        self.assertIsNotNone(state["status_id"])
        self.assertEqual(self.fake_bot.messages[-1]["reply_markup"].inline_keyboard[0][0].callback_data,
                         "zipstop:" + state["job_id"])
        stop = FakeQuery("zipstop:" + state["job_id"])
        stop.from_user.id = 7
        stop.message.message_id = state["status_id"]
        await bot.stop_zip_archive(SimpleNamespace(callback_query=stop), self.context)
        gate.release()
        await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(self.fake_bot.documents, [])
        self.assertIn("ZIP остановлен", self.fake_bot.edits[-1]["text"])
        self.assertIsNone(self.fake_bot.edits[-1]["reply_markup"])

    async def test_owner_can_download_own_zip_without_copying_tracks_to_history(self):
        self.seed_shared_tracks(1)
        self.fake_bot.files["saved-0"] = b"audio"
        token = bot.get_or_create_share_token(7)
        query = FakeQuery("zip:" + token)
        query.message.chat_id = 7
        await bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context)
        await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(len(self.fake_bot.documents), 1)
        self.assertEqual(self.fake_bot.documents[0]["chat_id"], 7)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sent_tracks WHERE chat_id=7").fetchone()[0], 1)

    async def test_zip_reports_oversize_file_and_rejects_revoked_or_group_callback(self):
        self.seed_shared_tracks(2)
        self.fake_bot.files["saved-0"] = (b"", bot.ZIP_FETCH_LIMIT_BYTES + 1)
        self.fake_bot.files["saved-1"] = b"available"
        token = bot.get_or_create_share_token(7)
        query = FakeQuery("zip:" + token)
        query.message.chat_id = 8
        await bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context)
        await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(len(self.fake_bot.documents), 1)
        self.assertIn("1/2", self.fake_bot.edits[-1]["text"])
        self.assertTrue(any("Song 000" in message["text"] for message in self.fake_bot.messages))
        await bot.unshare_command(self.private_command_update(chat_id=7)[0], self.context)
        revoked = FakeQuery("zip:" + token)
        revoked.message.chat_id = 8
        await bot.zip_archive_action(SimpleNamespace(callback_query=revoked), self.context)
        self.assertIn("не действует", revoked.answers[-1][0][0])
        group = FakeQuery("zip:" + token)
        group.message.chat.type = "supergroup"
        await bot.zip_archive_action(SimpleNamespace(callback_query=group), self.context)
        self.assertIn("личном чате", group.answers[-1][0][0])
        self.assertEqual(len(self.fake_bot.documents), 1)

    async def test_zip_five_clicks_queue_globally_and_same_user_cannot_duplicate(self):
        bot.record_sent_track(7, "https://www.youtube.com/watch?v=00000000001", "Artist", "Song", file_id="one")
        self.fake_bot.files["one"] = b"audio"
        token = bot.get_or_create_share_token(7)
        entered = asyncio.Event()
        release = asyncio.Event()
        original_send = self.fake_bot.send_document
        concurrent = peak = 0

        async def gated_send(**kwargs):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            entered.set()
            await release.wait()
            try:
                return await original_send(**kwargs)
            finally:
                concurrent -= 1

        self.fake_bot.send_document = gated_send
        queries = [FakeQuery("zip:" + token) for _ in range(5)]
        for chat_id, query in zip(range(8, 13), queries):
            query.message.chat_id = chat_id
        await asyncio.gather(*(
            bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context) for query in queries
        ))
        await asyncio.wait_for(entered.wait(), 1)
        duplicate = FakeQuery("zip:" + token)
        duplicate.message.chat_id = 8
        await bot.zip_archive_action(SimpleNamespace(callback_query=duplicate), self.context)
        self.assertIn("уже готовится", duplicate.answers[-1][0][0])
        self.assertEqual(peak, 1)
        release.set()
        await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(peak, 1)
        self.assertEqual(len(self.fake_bot.documents), 5)
        self.assertEqual({part["chat_id"] for part in self.fake_bot.documents}, set(range(8, 13)))

    async def test_zip_retries_timed_out_fetch_and_flood_limited_upload(self):
        bot.record_sent_track(7, "https://www.youtube.com/watch?v=00000000001", "Artist", "Song", file_id="saved")
        self.fake_bot.files["saved"] = b"audio"
        token = bot.get_or_create_share_token(7)
        original_get = self.fake_bot.get_file
        original_send = self.fake_bot.send_document
        fetches = uploads = 0

        async def get_with_timeouts(*args, **kwargs):
            nonlocal fetches
            fetches += 1
            if fetches < 3:
                raise bot.NetworkError("Timed out")
            return await original_get(*args, **kwargs)

        async def send_with_flood_limit(**kwargs):
            nonlocal uploads
            uploads += 1
            if uploads == 1:
                raise bot.RetryAfter(0)
            return await original_send(**kwargs)

        self.fake_bot.get_file = get_with_timeouts
        self.fake_bot.send_document = send_with_flood_limit
        query = FakeQuery("zip:" + token)
        query.message.chat_id = 8
        await bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context)
        await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(fetches, 3)
        self.assertEqual(uploads, 2)
        self.assertEqual(len(self.fake_bot.documents), 1)
        self.assertIn("1/1", self.fake_bot.edits[-1]["text"])

    async def test_unshare_stops_zip_before_next_part(self):
        self.seed_shared_tracks(2)
        self.fake_bot.files["saved-0"] = b"a" * 2000
        self.fake_bot.files["saved-1"] = b"b" * 2000
        token = bot.get_or_create_share_token(7)
        entered = asyncio.Event()
        release = asyncio.Event()
        original_send = self.fake_bot.send_document

        async def held_upload(**kwargs):
            entered.set()
            await release.wait()
            return await original_send(**kwargs)

        self.fake_bot.send_document = held_upload
        query = FakeQuery("zip:" + token)
        query.message.chat_id = 8
        with patch.object(bot, "ZIP_PART_TARGET_BYTES", 3000):
            await bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context)
            await asyncio.wait_for(entered.wait(), 1)
            owner, _ = self.private_command_update(chat_id=7)
            revoke = asyncio.create_task(bot.unshare_command(owner, self.context))
            await asyncio.sleep(0)
            release.set()
            await revoke
            await asyncio.gather(*self.context.application.tasks)
        self.assertLessEqual(len(self.fake_bot.documents), 1)
        self.assertIn("отозвана", self.fake_bot.edits[-1]["text"])

    async def test_zip_continues_after_initial_and_midway_status_edit_errors(self):
        self.seed_shared_tracks(2)
        self.fake_bot.files["saved-0"] = b"a" * 2000
        self.fake_bot.files["saved-1"] = b"b" * 2000
        token = bot.get_or_create_share_token(7)
        original_edit = self.fake_bot.edit_message_text
        edit_calls = 0

        async def unreliable_edit(**kwargs):
            nonlocal edit_calls
            edit_calls += 1
            if edit_calls == 1:
                raise bot.RetryAfter(0)
            if edit_calls == 3:
                raise bot.TelegramError("temporary edit failure")
            return await original_edit(**kwargs)

        self.fake_bot.edit_message_text = unreliable_edit
        query = FakeQuery("zip:" + token)
        query.message.chat_id = 8
        with patch.object(bot, "ZIP_PART_TARGET_BYTES", 3000):
            await bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context)
            await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(len(self.fake_bot.documents), 2)
        self.assertIn("2/2", self.fake_bot.edits[-1]["text"])

    async def test_zip_does_not_resend_after_ambiguous_upload_timeout(self):
        self.seed_shared_tracks(2)
        self.fake_bot.files["saved-0"] = b"a" * 2000
        self.fake_bot.files["saved-1"] = b"b" * 2000
        token = bot.get_or_create_share_token(7)
        original_send = self.fake_bot.send_document
        attempts = 0

        async def delivered_then_timeout(**kwargs):
            nonlocal attempts
            attempts += 1
            await original_send(**kwargs)
            raise bot.NetworkError("response timed out")

        self.fake_bot.send_document = delivered_then_timeout
        query = FakeQuery("zip:" + token)
        query.message.chat_id = 8
        with patch.object(bot, "ZIP_PART_TARGET_BYTES", 3000):
            await bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context)
            await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(attempts, 1)
        self.assertEqual(len(self.fake_bot.documents), 1)
        self.assertIn("часть могла дойти", self.fake_bot.edits[-1]["text"])
        self.assertIn("Полная доставка не подтверждена", self.fake_bot.edits[-1]["text"])

    async def test_failure_list_message_error_keeps_successful_zip_status_and_hides_details(self):
        self.seed_shared_tracks(2)
        self.fake_bot.files["saved-0"] = bot.BadRequest("private internal error text")
        self.fake_bot.files["saved-1"] = b"available"
        token = bot.get_or_create_share_token(7)
        original_message = self.fake_bot.send_message
        calls = 0

        async def fail_failure_list(**kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise bot.TelegramError("failure list unavailable")
            return await original_message(**kwargs)

        self.fake_bot.send_message = fail_failure_list
        query = FakeQuery("zip:" + token)
        query.message.chat_id = 8
        await bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context)
        await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(len(self.fake_bot.documents), 1)
        self.assertEqual(calls, 2)
        summary = self.fake_bot.edits[-1]["text"]
        self.assertIn("ZIP готов: 1/2 MP3", summary)
        self.assertIn("Song 000", summary)
        self.assertIn("Telegram не дал получить MP3", summary)
        self.assertNotIn("private internal error text", summary)
        self.assertNotIn("ZIP прерван", summary)

    async def test_zip_command_exports_own_library_without_share_token_in_same_topic(self):
        self.seed_shared_tracks(2)
        self.fake_bot.files["saved-0"] = b"first"
        self.fake_bot.files["saved-1"] = b"second"
        update, message = self.private_command_update(chat_id=7)
        message.message_thread_id = 88
        with patch.object(bot, "ZIP_SUCCESS_STATUS_SECONDS", 0):
            await bot.zip_command(update, self.context)
            await asyncio.gather(*self.context.application.tasks)
            await asyncio.sleep(0.01)
        message.reply_text.assert_not_awaited()
        self.assertEqual(len(self.fake_bot.documents), 1)
        self.assertEqual(self.fake_bot.documents[0]["chat_id"], 7)
        self.assertEqual(self.fake_bot.documents[0]["message_thread_id"], 88)
        self.assertEqual(self.fake_bot.messages[0]["message_thread_id"], 88)
        self.assertEqual(len(self.fake_bot.messages), 1)
        self.assertEqual(self.fake_bot.deleted, [200, 401])
        with zipfile.ZipFile(io.BytesIO(self.fake_bot.documents[0]["content"])) as archive:
            self.assertEqual(len(archive.namelist()), 2)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shared_playlists").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sent_tracks WHERE chat_id=7").fetchone()[0], 2)

    async def test_zip_failure_notice_and_status_are_transient_but_archive_remains(self):
        self.seed_shared_tracks(2)
        self.fake_bot.files["saved-0"] = bot.BadRequest("file unavailable")
        self.fake_bot.files["saved-1"] = b"available"
        query = FakeQuery("zip:own")
        query.from_user.id = 7
        with patch.object(bot, "ZIP_ERROR_STATUS_SECONDS", 0):
            await bot.zip_archive_action(SimpleNamespace(callback_query=query), self.context)
            await asyncio.gather(*self.context.application.tasks)
            await asyncio.sleep(0.01)
        self.assertEqual(len(self.fake_bot.documents), 1)
        self.assertEqual(len(self.fake_bot.messages), 2)
        self.assertIn("Не вошли в ZIP", self.fake_bot.messages[1]["text"])
        self.assertEqual(set(self.fake_bot.deleted), {401, 402})

    async def test_duplicate_zip_request_cleans_up_notice(self):
        active = asyncio.create_task(asyncio.sleep(1))
        self.context.application.bot_data["zip_archive_tasks"] = {7: active}
        update, message = self.private_command_update(chat_id=7)
        try:
            with patch.object(bot, "TRANSIENT_REPLY_SECONDS", 0):
                await bot.zip_command(update, self.context)
                await asyncio.sleep(0.01)
            self.assertIn("уже готовится", message.reply_text.await_args.args[0])
            self.assertEqual(self.fake_bot.deleted, [200, 300])
        finally:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)

    async def test_artist_library_zip_button_survives_navigation_without_share(self):
        self.seed_shared_tracks(10)
        for number in range(10):
            self.fake_bot.files[f"saved-{number}"] = f"audio-{number}".encode()
        update, message = self.private_command_update(chat_id=7)
        await bot.artists_command(update, self.context)
        markup = message.reply_text.call_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data, "zip:own")
        artist_query = FakeQuery(markup.inline_keyboard[0][0].callback_data)
        artist_query.from_user.id = 7
        await bot.library_action(SimpleNamespace(callback_query=artist_query), self.context)
        artist_markup = artist_query.edits[-1][1]
        self.assertEqual(artist_markup.inline_keyboard[-1][0].text, "◀️ К исполнителям")
        back_query = FakeQuery(artist_markup.inline_keyboard[-1][0].callback_data)
        back_query.from_user.id = 7
        await bot.library_action(SimpleNamespace(callback_query=back_query), self.context)
        self.assertEqual(back_query.edits[-1][1].inline_keyboard[-1][0].callback_data, "zip:own")
        own_query = FakeQuery("zip:own")
        own_query.from_user.id = 7
        own_query.message.message_thread_id = 77
        await bot.zip_archive_action(SimpleNamespace(callback_query=own_query), self.context)
        await asyncio.gather(*self.context.application.tasks)
        self.assertEqual(len(self.fake_bot.documents), 2)
        self.assertTrue(all(document["message_thread_id"] == 77 for document in self.fake_bot.documents))
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shared_playlists").fetchone()[0], 0)

    async def test_own_zip_rejects_groups_and_foreign_user_without_leaking_tracks(self):
        self.seed_shared_tracks(1)
        self.fake_bot.files["saved-0"] = b"secret audio"
        group, group_message = self.private_command_update(chat_id=-7, chat_type="supergroup")
        await bot.zip_command(group, self.context)
        self.assertIn("только в личном чате", group_message.reply_text.call_args.args[0])
        foreign = FakeQuery("zip:own")
        foreign.message.chat_id = 7
        foreign.from_user.id = 8
        await bot.zip_archive_action(SimpleNamespace(callback_query=foreign), self.context)
        self.assertIn("чужая библиотека", foreign.answers[-1][0][0])
        group_button = FakeQuery("zip:own")
        group_button.message.chat.type = "supergroup"
        await bot.zip_archive_action(SimpleNamespace(callback_query=group_button), self.context)
        self.assertIn("личном чате", group_button.answers[-1][0][0])
        self.assertEqual(self.context.application.tasks, [])
        self.assertEqual(self.fake_bot.documents, [])
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shared_playlists").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
