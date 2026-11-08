# Установка и локальный запуск

Все команды выполняются из корня репозитория — каталога с `bot.py` и `requirements.txt`.

## Что понадобится

- Python 3.10 или новее;
- `ffmpeg` и `ffprobe` для конвертации;
- `deno` для работы актуального `yt-dlp` с YouTube;
- токен бота от [@BotFather](https://t.me/BotFather).

На macOS зависимости можно поставить так:

```bash
brew install python ffmpeg deno
```

На Ubuntu/Debian установите Python, `python3-venv` и `ffmpeg` пакетным менеджером, а Deno — по [официальной инструкции](https://docs.deno.com/runtime/getting_started/installation/). На Windows установите Python, ffmpeg и Deno и добавьте их в `PATH`.

## Запуск

1. Создайте бота у @BotFather командой `/newbot` и сохраните токен.
2. Скачайте проект и установите зависимости:

   ```bash
   git clone https://github.com/zhanb6/Musica-telegram-bot.git
   cd Musica-telegram-bot
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

   На Windows создайте окружение командой `py -3 -m venv .venv`. Для активации в PowerShell выполните `.\.venv\Scripts\Activate.ps1`, в `cmd` — `.venv\Scripts\activate.bat`. Затем установите зависимости той же командой `python -m pip install -r requirements.txt`.

3. Скопируйте `.env.example` в `.env` и впишите свой токен после `TELEGRAM_BOT_TOKEN=`:

   ```bash
   cp .env.example .env
   ```

   На Windows файл `.env` можно создать вручную рядом с `bot.py`.

4. Запустите бота:

   ```bash
   python bot.py
   ```

5. Откройте бота в Telegram и нажмите «Начать». Выберите «🎵 Скачать музыку» → «🔗 По ссылке» или «🔎 Найти песню». Для поиска достаточно написать название обычным сообщением.

Бот работает, пока запущен `python bot.py` и компьютер подключён к интернету. История песен и избранное хранятся в `downloads.sqlite3` рядом с `bot.py`. При переносе того же бота на другой компьютер сохраните этот файл и `.env`. Они исключены из Git; токен в репозиторий не добавляйте.

Для автоматического отслеживания реакции ❤️ в личном чате добавьте в `.env` свои `TELEGRAM_API_ID` и `TELEGRAM_API_HASH`. Их получают в разделе API development tools на `my.telegram.org`. Бот создаёт отдельный сеанс для своего токена рядом с базой данных; вход в личный аккаунт Telegram ему не нужен. Без этих двух параметров команды `/favorite` и `/unfavorite` продолжают работать, но ручные реакции не отслеживаются.

## Обновление

Остановите бота, затем выполните:

```bash
git pull
source .venv/bin/activate
python -m pip install -r requirements.txt
python bot.py
```

## Проверка проекта

```bash
python -m unittest discover -s tests
```

Стандартная команда `python -m unittest` также находит весь набор тестов. Отдельный сценарий можно запустить по имени, например:

```bash
python -m unittest tests.test_bot_workflows.BotWorkflowTests.test_navigation_start_submenu_back_and_cancel
```

[← На главную](../README.md) · [Использование](usage.md) · [Docker](deployment.md)
