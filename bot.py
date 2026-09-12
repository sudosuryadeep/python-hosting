import asyncio
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from datetime import datetime

import psutil
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

PROJECTS_DIR = BASE_DIR / "projects"
DATA_DIR = BASE_DIR / "data"

DB_FILE = DATA_DIR / "projects.json"

VENV_DIR = BASE_DIR / "venv"
PYTHON_BIN = VENV_DIR / "bin" / "python"
PIP_BIN = VENV_DIR / "bin" / "pip"

PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# HOSTING BOT CONFIG
# ============================================================

load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in .env")

if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("ADMIN_ID missing/invalid in .env")

ADMIN_ID = int(ADMIN_ID_RAW)


# ============================================================
# BOT
# ============================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# DATABASE
# ============================================================

def load_db():
    if not DB_FILE.exists():
        return {}

    try:
        return json.loads(
            DB_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def save_db(data):
    temp_file = DB_FILE.with_suffix(".tmp")

    temp_file.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    temp_file.replace(DB_FILE)


db = load_db()


# ============================================================
# HELPERS
# ============================================================

def user_key(user_id: int):
    return str(user_id)


def user_projects(user_id: int):
    uid = user_key(user_id)

    if uid not in db:
        db[uid] = {}

    return db[uid]


def safe_name(name: str):

    name = Path(name).stem

    name = re.sub(
        r"[^a-zA-Z0-9_-]",
        "_",
        name
    )

    name = name.strip("_")

    if not name:
        name = "project"

    return name[:50]


def project_dir(user_id: int, project_name: str):
    return (
        PROJECTS_DIR
        / str(user_id)
        / project_name
    )


def project_meta(user_id: int, project_name: str):

    projects = user_projects(user_id)

    return projects.get(project_name)


def pid_file(user_id: int, project_name: str):

    return (
        project_dir(user_id, project_name)
        / ".pid"
    )


def log_file_path(user_id: int, project_name: str):

    return (
        project_dir(user_id, project_name)
        / "bot.log"
    )


def get_pid(user_id: int, project_name: str):

    path = pid_file(
        user_id,
        project_name
    )

    if not path.exists():
        return None

    try:

        pid = int(
            path.read_text().strip()
        )

        if psutil.pid_exists(pid):

            try:

                process = psutil.Process(pid)

                if process.status() != psutil.STATUS_ZOMBIE:
                    return pid

            except Exception:
                pass

    except Exception:
        pass

    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass

    return None


def is_running(user_id: int, project_name: str):

    return get_pid(
        user_id,
        project_name
    ) is not None


def html_escape(text: str):

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ============================================================
# ACCESS CONTROL
# ============================================================

def allowed(user_id: int):

    return user_id == ADMIN_ID


async def access_denied(message: Message):

    await message.answer(
        "❌ You are not authorized to use this hosting panel."
    )


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="📤 Upload Python",
                    callback_data="upload_help"
                )
            ],

            [
                InlineKeyboardButton(
                    text="📦 Projects",
                    callback_data="projects"
                ),

                InlineKeyboardButton(
                    text="🖥 Server",
                    callback_data="server"
                )
            ],

            [
                InlineKeyboardButton(
                    text="🔄 Update from GitHub",
                    callback_data="github_update"
                )
            ],

            [
                InlineKeyboardButton(
                    text="❓ Help",
                    callback_data="help"
                )
            ]
        ]
    )


def project_keyboard(
    name: str,
    running: bool
):

    buttons = []

    if running:

        buttons.append(
            [
                InlineKeyboardButton(
                    text="⏹ Stop",
                    callback_data=f"stop:{name}"
                ),

                InlineKeyboardButton(
                    text="🔄 Restart",
                    callback_data=f"restart:{name}"
                )
            ]
        )

    else:

        buttons.append(
            [
                InlineKeyboardButton(
                    text="▶️ Start",
                    callback_data=f"start:{name}"
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="📜 Logs",
                callback_data=f"logs:{name}"
            ),

            InlineKeyboardButton(
                text="📊 Status",
                callback_data=f"status:{name}"
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                text="🗑 Delete",
                callback_data=f"delete:{name}"
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ Projects",
                callback_data="projects"
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


def project_list_keyboard(user_id: int):

    projects = user_projects(user_id)

    rows = []

    for name in projects:

        running = is_running(
            user_id,
            name
        )

        status = "🟢" if running else "🔴"

        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {name}",
                    callback_data=f"open:{name}"
                )
            ]
        )

    if not rows:

        rows.append(
            [
                InlineKeyboardButton(
                    text="No projects",
                    callback_data="noop"
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Home",
                callback_data="home"
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


# ============================================================
# COMMAND RUNNER
# ============================================================

async def run_command(
    command,
    cwd=None,
    env=None
):

    process = await asyncio.create_subprocess_exec(

        *command,

        cwd=str(cwd) if cwd else None,

        stdout=asyncio.subprocess.PIPE,

        stderr=asyncio.subprocess.STDOUT,

        env=env
    )

    output, _ = await process.communicate()

    return (
        process.returncode,
        output.decode(
            errors="replace"
        )
    )


# ============================================================
# SERVER STATUS
# ============================================================

async def server_status_text():

    cpu = psutil.cpu_percent(
        interval=0.5
    )

    ram = psutil.virtual_memory()

    disk = psutil.disk_usage("/")

    uptime_seconds = int(
        datetime.now().timestamp()
        - psutil.boot_time()
    )

    days = uptime_seconds // 86400

    hours = (
        uptime_seconds % 86400
    ) // 3600

    minutes = (
        uptime_seconds % 3600
    ) // 60

    return (
        "🖥 <b>Server Status</b>\n\n"

        f"⚙️ CPU: <code>{cpu}%</code>\n"

        f"🧠 RAM: <code>{ram.percent}%</code> "
        f"({ram.used // 1024**2} MB / "
        f"{ram.total // 1024**2} MB)\n"

        f"💾 Disk: <code>{disk.percent}%</code> "
        f"({disk.used // 1024**3} GB / "
        f"{disk.total // 1024**3} GB)\n"

        f"⏱ Uptime: "
        f"<code>{days}d {hours}h {minutes}m</code>"
    )


# ============================================================
# START COMMAND
# ============================================================

@dp.message(Command("start"))
async def start_handler(message: Message):

    if not allowed(message.from_user.id):

        return await access_denied(message)

    await message.answer(

        "🐍 <b>Python Hosting Panel</b>\n\n"

        "Upload your Python bot directly "
        "from Telegram.\n\n"

        "📤 Send a <code>.py</code> file "
        "to create a project.\n"

        "📦 Send <code>requirements.txt</code> "
        "to install dependencies.\n\n"

        "⚡ Manage everything from the buttons below.",

        reply_markup=main_keyboard(),

        parse_mode="HTML"
    )


# ============================================================
# HELP COMMAND
# ============================================================

@dp.message(Command("help"))
async def help_command(message: Message):

    if not allowed(message.from_user.id):

        return await access_denied(message)

    await message.answer(

        "🐍 <b>Python Hosting Help</b>\n\n"

        "1️⃣ Send a Python file\n"
        "<code>mybot.py</code>\n\n"

        "2️⃣ Send requirements if needed\n"
        "<code>requirements.txt</code>\n\n"

        "3️⃣ Open Projects\n\n"

        "4️⃣ Start your project\n\n"

        "🔄 GitHub Update:\n"
        "Use <b>Update from GitHub</b> "
        "to pull the latest hosting bot code.\n\n"

        "<b>Commands</b>\n"
        "/start\n"
        "/projects\n"
        "/server\n"
        "/help",

        parse_mode="HTML"
    )


# ============================================================
# PROJECTS COMMAND
# ============================================================

@dp.message(Command("projects"))
async def projects_command(message: Message):

    if not allowed(message.from_user.id):

        return await access_denied(message)

    projects = user_projects(
        message.from_user.id
    )

    if not projects:

        await message.answer(

            "📦 <b>No projects yet.</b>\n\n"
            "Send a <code>.py</code> file "
            "to create one.",

            parse_mode="HTML"
        )

        return

    await message.answer(

        "📦 <b>Your Projects</b>",

        reply_markup=project_list_keyboard(
            message.from_user.id
        ),

        parse_mode="HTML"
    )


# ============================================================
# UPLOAD HELP
# ============================================================

@dp.callback_query(F.data == "upload_help")
async def upload_help_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    await callback.message.answer(

        "📤 <b>Upload Project</b>\n\n"

        "Send your Python file directly:\n"
        "🐍 <code>metadata_bot.py</code>\n\n"

        "Then send:\n"
        "📦 <code>requirements.txt</code>\n\n"

        "After that open Projects and press "
        "▶️ Start.",

        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# DOCUMENT UPLOAD
# ============================================================

@dp.message(F.document)
async def document_handler(message: Message):

    if not allowed(
        message.from_user.id
    ):

        return await access_denied(message)

    document = message.document

    filename = document.file_name or ""

    lower = filename.lower()

    user_id = message.from_user.id

    projects = user_projects(
        user_id
    )


    # ========================================================
    # PYTHON
    # ========================================================

    if lower.endswith(".py"):

        project_name = safe_name(
            filename
        )

        if project_name in projects:

            await message.answer(

                f"⚠️ Project "
                f"<code>{project_name}</code> "
                f"already exists.\n\n"

                "Delete the old project first "
                "or rename the file.",

                parse_mode="HTML"
            )

            return

        pdir = project_dir(
            user_id,
            project_name
        )

        pdir.mkdir(
            parents=True,
            exist_ok=True
        )

        py_path = (
            pdir
            / Path(filename).name
        )

        status_message = await message.answer(

            "📥 <b>Downloading project...</b>\n\n"
            f"📦 {html_escape(project_name)}\n"
            "🐍 Downloading Python file...",

            parse_mode="HTML"
        )

        try:

            file = await bot.get_file(
                document.file_id
            )

            await bot.download_file(
                file.file_path,
                destination=py_path
            )

        except Exception as e:

            await status_message.edit_text(

                "❌ Download failed.\n\n"
                f"<pre>{html_escape(str(e))}</pre>",

                parse_mode="HTML"
            )

            return

        projects[project_name] = {

            "entry": py_path.name,

            "created":
                datetime.now().isoformat(),

            "requirements": False

        }

        save_db(db)

        await status_message.edit_text(

            "✅ <b>Project created</b>\n\n"

            f"📦 Name: "
            f"<code>{html_escape(project_name)}</code>\n"

            f"🐍 Entry: "
            f"<code>{html_escape(py_path.name)}</code>\n\n"

            "📦 If dependencies are needed, "
            "send <code>requirements.txt</code>.\n\n"

            "Then press ▶️ Start.",

            reply_markup=project_keyboard(
                project_name,
                False
            ),

            parse_mode="HTML"
        )

        return


    # ========================================================
    # REQUIREMENTS
    # ========================================================

    if lower == "requirements.txt":

        if not projects:

            await message.answer(

                "❌ Create a Python project first "
                "by sending a .py file."
            )

            return

        if len(projects) != 1:

            await message.answer(

                "⚠️ Multiple projects exist.\n\n"

                "Currently requirements.txt "
                "can automatically attach only "
                "when there is one project.\n\n"

                "Open one project first or "
                "use a single-project setup."
            )

            return

        project_name = list(
            projects.keys()
        )[0]

        pdir = project_dir(
            user_id,
            project_name
        )

        requirements_path = (
            pdir / "requirements.txt"
        )

        file = await bot.get_file(
            document.file_id
        )

        await bot.download_file(
            file.file_path,
            destination=requirements_path
        )

        projects[project_name][
            "requirements"
        ] = True

        save_db(db)

        await install_requirements_live(
            message,
            project_name,
            requirements_path
        )

        return


    # ========================================================
    # ENV
    # ========================================================

    if lower == ".env":

        await message.answer(

            "⚠️ Project <code>.env</code> upload "
            "is disabled.\n\n"

            "Use environment variables through "
            "the hosting configuration instead.",

            parse_mode="HTML"
        )

        return


    # ========================================================
    # UNKNOWN
    # ========================================================

    await message.answer(

        "❌ Unsupported file.\n\n"

        "Supported:\n"
        "🐍 <code>.py</code>\n"
        "📦 <code>requirements.txt</code>",

        parse_mode="HTML"
    )


        # ============================================================
# LIVE REQUIREMENTS INSTALL
# ============================================================

async def install_requirements_live(
    message: Message,
    project_name: str,
    requirements_path: Path
):

    start_time = time.time()

    status = await message.answer(

        "📦 <b>Installing requirements</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        f"🤖 Project: "
        f"<code>{html_escape(project_name)}</code>\n\n"

        "🔄 Starting pip...\n\n"

        "⏱ Elapsed: <code>0s</code>",

        parse_mode="HTML"
    )

    process = await asyncio.create_subprocess_exec(

        str(PIP_BIN),

        "install",

        "-r",

        str(requirements_path),

        "--progress-bar",
        "off",

        stdout=asyncio.subprocess.PIPE,

        stderr=asyncio.subprocess.STDOUT
    )

    output_lines = []

    last_update = 0

    while True:

        line = await process.stdout.readline()

        if not line:
            break

        decoded = line.decode(
            errors="replace"
        ).strip()

        if decoded:

            output_lines.append(
                decoded
            )

            output_lines = output_lines[-12:]


        now = time.time()

        if now - last_update >= 2:

            last_update = now

            elapsed = int(
                now - start_time
            )

            recent = "\n".join(
                output_lines[-6:]
            )

            if len(recent) > 2200:

                recent = recent[-2200:]

            text = (

                "📦 <b>Installing requirements</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"

                f"🤖 Project: "
                f"<code>{html_escape(project_name)}</code>\n\n"

                f"🔄 <b>pip output</b>\n"

                f"<pre>{html_escape(recent)}</pre>\n\n"

                f"⏱ Elapsed: "
                f"<code>{elapsed}s</code>\n\n"

                "⚙️ Installation in progress..."
            )

            try:

                await status.edit_text(
                    text,
                    parse_mode="HTML"
                )

            except TelegramBadRequest:
                pass

    returncode = await process.wait()

    elapsed = round(
        time.time() - start_time,
        1
    )

    if returncode == 0:

        recent = "\n".join(
            output_lines[-8:]
        )

        if len(recent) > 2500:
            recent = recent[-2500:]

        await status.edit_text(

            "✅ <b>Installation Complete</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            f"🤖 Project: "
            f"<code>{html_escape(project_name)}</code>\n\n"

            f"⏱ Time: "
            f"<code>{elapsed}s</code>\n\n"

            f"<pre>{html_escape(recent)}</pre>\n\n"

            "🚀 <b>Project is ready!</b>",

            reply_markup=project_keyboard(
                project_name,
                False
            ),

            parse_mode="HTML"
        )

    else:

        recent = "\n".join(
            output_lines[-12:]
        )

        if len(recent) > 3000:
            recent = recent[-3000:]

        await status.edit_text(

            "❌ <b>Installation Failed</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            f"🤖 Project: "
            f"<code>{html_escape(project_name)}</code>\n\n"

            f"⏱ Time: "
            f"<code>{elapsed}s</code>\n\n"

            f"<pre>{html_escape(recent)}</pre>",

            parse_mode="HTML"
        )


# ============================================================
# START PROJECT
# ============================================================

async def start_project(
    user_id: int,
    name: str
):

    meta = project_meta(
        user_id,
        name
    )

    if not meta:

        return False, "Project not found."

    if is_running(
        user_id,
        name
    ):

        return False, "Project is already running."

    pdir = project_dir(
        user_id,
        name
    )

    entry = meta.get("entry")

    if not entry:

        return False, "Entry file missing."

    entry_path = pdir / entry

    if not entry_path.exists():

        return False, "Python entry file not found."

    log_path = log_file_path(
        user_id,
        name
    )

    log = open(
        log_path,
        "a",
        encoding="utf-8"
    )

    log.write(
        "\n\n"
        f"===== START {datetime.now().isoformat()} =====\n"
    )

    log.flush()

    env = os.environ.copy()

    # ========================================================
    # IMPORTANT:
    # No project .env loading.
    # ========================================================

    try:

        process = await asyncio.create_subprocess_exec(

            str(PYTHON_BIN),

            entry,

            cwd=str(pdir),

            stdout=log,

            stderr=log,

            env=env,

            start_new_session=True
        )

        pid_path = pid_file(
            user_id,
            name
        )

        pid_path.write_text(
            str(process.pid),
            encoding="utf-8"
        )

        # We intentionally don't close the file immediately
        # because child process inherits the descriptor.
        # The OS will close it when the hosting bot exits.
        return True, process.pid

    except Exception as e:

        log.close()

        return False, str(e)


# ============================================================
# STOP PROJECT
# ============================================================

async def stop_project(
    user_id: int,
    name: str
):

    pid = get_pid(
        user_id,
        name
    )

    if not pid:

        return False, "Project is not running."

    try:

        process = psutil.Process(pid)

        try:

            os.killpg(
                os.getpgid(pid),
                signal.SIGTERM
            )

        except Exception:

            process.terminate()

        try:

            process.wait(
                timeout=8
            )

        except psutil.TimeoutExpired:

            try:

                os.killpg(
                    os.getpgid(pid),
                    signal.SIGKILL
                )

            except Exception:

                try:
                    process.kill()
                except Exception:
                    pass

        pid_file(
            user_id,
            name
        ).unlink(
            missing_ok=True
        )

        return True, "Stopped."

    except psutil.NoSuchProcess:

        pid_file(
            user_id,
            name
        ).unlink(
            missing_ok=True
        )

        return True, "Already stopped."

    except Exception as e:

        return False, str(e)


# ============================================================
# PROJECT STATUS
# ============================================================

def project_status_text(
    user_id: int,
    name: str
):

    meta = project_meta(
        user_id,
        name
    )

    if not meta:
        return "❌ Project not found."

    running = is_running(
        user_id,
        name
    )

    pid = get_pid(
        user_id,
        name
    )

    pdir = project_dir(
        user_id,
        name
    )

    entry = meta.get(
        "entry",
        "unknown"
    )

    requirements = (
        "✅ Installed"
        if meta.get("requirements")
        else "➖ None"
    )

    if running and pid:

        try:

            process = psutil.Process(pid)

            cpu = process.cpu_percent(
                interval=0.1
            )

            memory = (
                process.memory_info().rss
                / 1024
                / 1024
            )

            status = "🟢 Running"

            stats = (

                f"🔢 PID: "
                f"<code>{pid}</code>\n"

                f"⚙️ CPU: "
                f"<code>{cpu:.1f}%</code>\n"

                f"🧠 RAM: "
                f"<code>{memory:.1f} MB</code>"
            )

        except Exception:

            status = "🟢 Running"

            stats = (
                f"🔢 PID: <code>{pid}</code>"
            )

    else:

        status = "🔴 Stopped"

        stats = ""

    return (

        f"📦 <b>{html_escape(name)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        f"{status}\n\n"

        f"🐍 Entry: "
        f"<code>{html_escape(entry)}</code>\n"

        f"📦 Requirements: "
        f"{requirements}\n\n"

        f"{stats}"
    )


# ============================================================
# LOGS
# ============================================================

def read_logs(
    user_id: int,
    name: str,
    limit=3500
):

    path = log_file_path(
        user_id,
        name
    )

    if not path.exists():

        return "No logs yet."

    try:

        text = path.read_text(
            encoding="utf-8",
            errors="replace"
        )

        return text[-limit:]

    except Exception as e:

        return str(e)


# ============================================================
# PROJECT CALLBACKS
# ============================================================

@dp.callback_query(F.data == "projects")
async def projects_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    projects = user_projects(
        callback.from_user.id
    )

    text = (
        "📦 <b>Your Projects</b>\n\n"
        "🟢 Running\n"
        "🔴 Stopped"
    )

    try:

        await callback.message.edit_text(
            text,
            reply_markup=project_list_keyboard(
                callback.from_user.id
            ),
            parse_mode="HTML"
        )

    except TelegramBadRequest:

        await callback.message.answer(
            text,
            reply_markup=project_list_keyboard(
                callback.from_user.id
            ),
            parse_mode="HTML"
        )

    await callback.answer()


@dp.callback_query(F.data == "home")
async def home_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    await callback.message.edit_text(

        "🐍 <b>Python Hosting Panel</b>\n\n"
        "Choose an action:",

        reply_markup=main_keyboard(),

        parse_mode="HTML"
    )

    await callback.answer()


@dp.callback_query(F.data == "server")
async def server_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    text = await server_status_text()

    await callback.message.edit_text(

        text,

        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Refresh",
                        callback_data="server"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Home",
                        callback_data="home"
                    )
                ]
            ]
        ),

        parse_mode="HTML"
    )

    await callback.answer()


@dp.callback_query(F.data == "help")
async def help_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    await callback.message.edit_text(

        "🐍 <b>Python Hosting Help</b>\n\n"

        "📤 Send <code>.py</code> "
        "to create a project.\n\n"

        "📦 Send <code>requirements.txt</code> "
        "to install dependencies.\n\n"

        "▶️ Start / ⏹ Stop / 🔄 Restart\n"
        "📜 Logs / 📊 Status\n\n"

        "🔄 Use <b>Update from GitHub</b> "
        "to update the hosting panel.",

        reply_markup=main_keyboard(),

        parse_mode="HTML"
    )

    await callback.answer()


@dp.callback_query(F.data == "noop")
async def noop_callback(
    callback: CallbackQuery
):

    await callback.answer(
        "No projects yet."
    )


# ============================================================
# OPEN PROJECT
# ============================================================

@dp.callback_query(
    F.data.startswith("open:")
)
async def open_project_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    name = callback.data.split(
        ":",
        1
    )[1]

    text = project_status_text(
        callback.from_user.id,
        name
    )

    await callback.message.edit_text(

        text,

        reply_markup=project_keyboard(
            name,
            is_running(
                callback.from_user.id,
                name
            )
        ),

        parse_mode="HTML"
    )

    await callback.answer()

# ============================================================
# START CALLBACK
# ============================================================

@dp.callback_query(
    F.data.startswith("start:")
)
async def start_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    name = callback.data.split(
        ":",
        1
    )[1]

    await callback.answer(
        "Starting..."
    )

    ok, result = await start_project(
        callback.from_user.id,
        name
    )

    if ok:

        text = project_status_text(
            callback.from_user.id,
            name
        )

        await callback.message.edit_text(

            "🚀 <b>Project Started</b>\n\n"
            + text,

            reply_markup=project_keyboard(
                name,
                True
            ),

            parse_mode="HTML"
        )

    else:

        await callback.message.answer(

            f"❌ Failed to start "
            f"<code>{html_escape(name)}</code>\n\n"
            f"<pre>{html_escape(str(result))}</pre>",

            parse_mode="HTML"
        )


# ============================================================
# STOP CALLBACK
# ============================================================

@dp.callback_query(
    F.data.startswith("stop:")
)
async def stop_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    name = callback.data.split(
        ":",
        1
    )[1]

    await callback.answer(
        "Stopping..."
    )

    ok, result = await stop_project(
        callback.from_user.id,
        name
    )

    text = project_status_text(
        callback.from_user.id,
        name
    )

    await callback.message.edit_text(

        (
            "⏹ <b>Project Stopped</b>\n\n"
            if ok
            else "❌ <b>Stop Failed</b>\n\n"
        )
        + text,

        reply_markup=project_keyboard(
            name,
            False
        ),

        parse_mode="HTML"
    )


# ============================================================
# RESTART CALLBACK
# ============================================================

@dp.callback_query(
    F.data.startswith("restart:")
)
async def restart_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    name = callback.data.split(
        ":",
        1
    )[1]

    await callback.answer(
        "Restarting..."
    )

    await stop_project(
        callback.from_user.id,
        name
    )

    await asyncio.sleep(1)

    ok, result = await start_project(
        callback.from_user.id,
        name
    )

    if ok:

        text = project_status_text(
            callback.from_user.id,
            name
        )

        await callback.message.edit_text(

            "🔄 <b>Project Restarted</b>\n\n"
            + text,

            reply_markup=project_keyboard(
                name,
                True
            ),

            parse_mode="HTML"
        )

    else:

        await callback.message.edit_text(

            "❌ <b>Restart Failed</b>\n\n"

            f"<pre>{html_escape(str(result))}</pre>",

            reply_markup=project_keyboard(
                name,
                False
            ),

            parse_mode="HTML"
        )


# ============================================================
# LOGS CALLBACK
# ============================================================

@dp.callback_query(
    F.data.startswith("logs:")
)
async def logs_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    name = callback.data.split(
        ":",
        1
    )[1]

    logs = read_logs(
        callback.from_user.id,
        name
    )

    if len(logs) > 3500:
        logs = logs[-3500:]

    await callback.message.answer(

        f"📜 <b>{html_escape(name)} logs</b>\n\n"
        f"<pre>{html_escape(logs)}</pre>",

        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# STATUS CALLBACK
# ============================================================

@dp.callback_query(
    F.data.startswith("status:")
)
async def status_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    name = callback.data.split(
        ":",
        1
    )[1]

    text = project_status_text(
        callback.from_user.id,
        name
    )

    await callback.message.edit_text(

        text,

        reply_markup=project_keyboard(
            name,
            is_running(
                callback.from_user.id,
                name
            )
        ),

        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# DELETE CALLBACK
# ============================================================

@dp.callback_query(
    F.data.startswith("delete:")
)
async def delete_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    name = callback.data.split(
        ":",
        1
    )[1]

    await callback.message.edit_text(

        f"⚠️ <b>Delete project?</b>\n\n"
        f"📦 <code>{html_escape(name)}</code>\n\n"
        "This will delete the project files "
        "and logs.",

        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[

                [
                    InlineKeyboardButton(
                        text="❌ Confirm Delete",
                        callback_data=f"confirm_delete:{name}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        text="⬅️ Cancel",
                        callback_data=f"open:{name}"
                    )
                ]
            ]
        ),

        parse_mode="HTML"
    )

    await callback.answer()


@dp.callback_query(
    F.data.startswith("confirm_delete:")
)
async def confirm_delete_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    name = callback.data.split(
        ":",
        1
    )[1]

    if is_running(
        callback.from_user.id,
        name
    ):

        await stop_project(
            callback.from_user.id,
            name
        )

    pdir = project_dir(
        callback.from_user.id,
        name
    )

    try:

        if pdir.exists():

            import shutil

            shutil.rmtree(
                pdir
            )

        projects = user_projects(
            callback.from_user.id
        )

        projects.pop(
            name,
            None
        )

        save_db(db)

        await callback.message.edit_text(

            "🗑 <b>Project deleted.</b>\n\n"
            f"📦 <code>{html_escape(name)}</code>",

            reply_markup=main_keyboard(),

            parse_mode="HTML"
        )

    except Exception as e:

        await callback.message.edit_text(

            "❌ Delete failed.\n\n"
            f"<pre>{html_escape(str(e))}</pre>",

            parse_mode="HTML"
        )

    await callback.answer()


# ============================================================
# GITHUB UPDATE
# ============================================================

async def github_update():

    returncode, output = await run_command(

        [
            "git",
            "pull",
            "--ff-only",
            "origin",
            "main"
        ],

        cwd=BASE_DIR
    )

    return returncode, output


@dp.callback_query(
    F.data == "github_update"
)
async def github_update_callback(
    callback: CallbackQuery
):

    if not allowed(
        callback.from_user.id
    ):

        return await callback.answer(
            "Unauthorized",
            show_alert=True
        )

    await callback.answer(
        "Updating..."
    )

    try:

        await callback.message.edit_text(

            "🔄 <b>Updating Hosting Bot</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "📡 Connecting to GitHub...\n"
            "⏳ Please wait...",

            parse_mode="HTML"
        )

        returncode, output = await github_update()

        output = output.strip()

        if len(output) > 3000:

            output = output[-3000:]

        if returncode != 0:

            await callback.message.edit_text(

                "❌ <b>GitHub update failed</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"

                f"<pre>{html_escape(output)}</pre>\n\n"

                "The bot was NOT restarted.",

                parse_mode="HTML"
            )

            return


        # ====================================================
        # RESTART
        # ====================================================

        await callback.message.edit_text(

            "✅ <b>GitHub Update Complete</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            f"<pre>{html_escape(output)}</pre>\n\n"

            "🔄 Restarting hosting bot...\n"
            "⏳ Please wait...",

            parse_mode="HTML"
        )

        await asyncio.sleep(2)

        # Replace current process with updated bot.py
        os.execv(
            sys.executable,
            [
                sys.executable,
                str(BASE_DIR / "bot.py")
            ]
        )

    except Exception as e:

        try:

            await callback.message.edit_text(

                "❌ <b>Update error</b>\n\n"
                f"<pre>{html_escape(str(e))}</pre>",

                parse_mode="HTML"
            )

        except Exception:
            pass


# ============================================================
# UNKNOWN CALLBACK
# ============================================================

@dp.callback_query()
async def unknown_callback(
    callback: CallbackQuery
):

    await callback.answer()


# ============================================================
# MAIN
# ============================================================

async def main():

    print(
        "🐍 Python Hosting Bot started"
    )

    print(
        f"📁 Base directory: {BASE_DIR}"
    )

    print(
        f"👤 Admin ID: {ADMIN_ID}"
    )

    await dp.start_polling(
        bot
    )


if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "🛑 Hosting bot stopped."
  )
