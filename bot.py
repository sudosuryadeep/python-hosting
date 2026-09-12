import asyncio
import json
import os
import re
import shutil
import signal
import time
import zipfile
from pathlib import Path
from datetime import datetime

import psutil
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.exceptions import TelegramBadRequest


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

PROJECTS_DIR = BASE_DIR / "projects"
DATA_DIR = BASE_DIR / "data"
TMP_DIR = BASE_DIR / "tmp"

DB_FILE = DATA_DIR / "projects.json"

PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

PAGE_SIZE = 35  # lines per page in the file viewer
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASS")

IGNORED_DIR_NAMES = {"venv", "__pycache__", ".git", "tmp"}
IGNORED_FILE_SUFFIXES = {".pyc"}
IGNORED_FILE_NAMES = {".pid"}


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
# BOT / FSM
# ============================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class EnvStates(StatesGroup):
    waiting_content = State()


class EditStates(StatesGroup):
    waiting_range = State()
    waiting_content = State()


# ============================================================
# DATABASE
# ============================================================

def load_db():
    if not DB_FILE.exists():
        return {}
    try:
        return json.loads(DB_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_db(data):
    temp_file = DB_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
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
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    name = name.strip("_")
    if not name:
        name = "project"
    return name[:50]


def project_dir(user_id: int, project_name: str):
    return PROJECTS_DIR / str(user_id) / project_name


def project_meta(user_id: int, project_name: str):
    return user_projects(user_id).get(project_name)


def pid_file(user_id: int, project_name: str):
    return project_dir(user_id, project_name) / ".pid"


def log_file_path(user_id: int, project_name: str):
    return project_dir(user_id, project_name) / "bot.log"


def get_pid(user_id: int, project_name: str):
    path = pid_file(user_id, project_name)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
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
    return get_pid(user_id, project_name) is not None


def html_escape(text: str):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def terminal_block(prompt: str, body: str, footer: str = ""):
    """Renders a fake-terminal look inside a Telegram <pre> block."""
    body = html_escape(body) if body else ""
    text = f"<pre>$ {html_escape(prompt)}\n{body}</pre>"
    if footer:
        text += f"\n{footer}"
    return text


def spinner_frame(tick: int):
    return SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]


def mask_env_line(line: str):
    if "=" not in line:
        return line
    key, _, value = line.partition("=")
    if any(hint in key.upper() for hint in SECRET_HINTS) and value.strip():
        visible = value.strip()[:3]
        return f"{key}={visible}{'*' * max(3, len(value.strip()) - 3)}"
    return line


def list_project_files(user_id: int, project_name: str):
    pdir = project_dir(user_id, project_name)
    files = []
    for root, dirs, filenames in os.walk(pdir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for fname in sorted(filenames):
            if fname in IGNORED_FILE_NAMES:
                continue
            if Path(fname).suffix in IGNORED_FILE_SUFFIXES:
                continue
            full = Path(root) / fname
            rel = full.relative_to(pdir)
            files.append(str(rel))
    return sorted(files)


def paginate_lines(lines, page: int, per_page: int = PAGE_SIZE):
    total_pages = max(1, (len(lines) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    chunk = lines[start:start + per_page]
    return chunk, page, total_pages, start


# ============================================================
# ACCESS CONTROL
# ============================================================

def allowed(user_id: int):
    return user_id == ADMIN_ID


async def access_denied(message: Message):
    await message.answer("🔒 Not authorized to use this hosting panel.")


async def access_denied_cb(callback: CallbackQuery):
    await callback.answer("🔒 Not authorized", show_alert=True)


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Upload", callback_data="upload_help"),
         InlineKeyboardButton(text="📦 Projects", callback_data="projects")],
        [InlineKeyboardButton(text="🖥  Server", callback_data="server"),
         InlineKeyboardButton(text="🔄 Update", callback_data="github_update")],
        [InlineKeyboardButton(text="❓ Help", callback_data="help")],
    ])


def project_keyboard(name: str, running: bool):
    rows = []

    if running:
        rows.append([
            InlineKeyboardButton(text="⏹  Stop", callback_data=f"stop:{name}"),
            InlineKeyboardButton(text="🔄 Restart", callback_data=f"restart:{name}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="▶️  Start", callback_data=f"start:{name}"),
        ])

    rows.append([
        InlineKeyboardButton(text="📜 Logs", callback_data=f"logs:{name}:0"),
        InlineKeyboardButton(text="📊 Status", callback_data=f"status:{name}"),
    ])

    rows.append([
        InlineKeyboardButton(text="📁 Files", callback_data=f"files:{name}"),
        InlineKeyboardButton(text="🌱 .env", callback_data=f"envmenu:{name}"),
    ])

    rows.append([
        InlineKeyboardButton(text="⬇️  Download .zip", callback_data=f"zip:{name}"),
    ])

    rows.append([
        InlineKeyboardButton(text="🗑  Delete", callback_data=f"askdelete:{name}"),
        InlineKeyboardButton(text="⬅️  Back", callback_data="projects"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def project_list_keyboard(user_id: int):
    projects = user_projects(user_id)
    rows = []

    for name in projects:
        running = is_running(user_id, name)
        dot = "🟢" if running else "⚪️"
        rows.append([InlineKeyboardButton(text=f"{dot}  {name}", callback_data=f"open:{name}")])

    if not rows:
        rows.append([InlineKeyboardButton(text="No projects yet", callback_data="noop")])

    rows.append([InlineKeyboardButton(text="⬅️  Home", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_delete_keyboard(name: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yes, delete", callback_data=f"dodelete:{name}"),
         InlineKeyboardButton(text="✖️ Cancel", callback_data=f"open:{name}")],
    ])


def file_list_keyboard(name: str, files):
    rows = []
    for idx, fname in enumerate(files):
        rows.append([InlineKeyboardButton(text=f"📄 {fname}", callback_data=f"vfile:{name}:{idx}:0")])
    rows.append([InlineKeyboardButton(text="⬅️  Back", callback_data=f"open:{name}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def file_view_keyboard(name: str, idx: int, page: int, total_pages: int):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"vfile:{name}:{idx}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"vfile:{name}:{idx}:{page + 1}"))

    rows = [nav] if nav else []
    rows.append([
        InlineKeyboardButton(text="✏️ Edit lines", callback_data=f"editlines:{name}:{idx}"),
        InlineKeyboardButton(text="⬇️ Download file", callback_data=f"dlfile:{name}:{idx}"),
    ])
    rows.append([InlineKeyboardButton(text="⬅️  Files", callback_data=f"files:{name}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def env_menu_keyboard(name: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Create / Replace", callback_data=f"envnew:{name}")],
        [InlineKeyboardButton(text="👁  View (masked)", callback_data=f"envview:{name}")],
        [InlineKeyboardButton(text="⬅️  Back", callback_data=f"open:{name}")],
    ])


# ============================================================
# SERVER STATUS
# ============================================================

async def server_status_text():
    cpu = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    uptime_seconds = int(datetime.now().timestamp() - psutil.boot_time())
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60

    body = (
        f"CPU     {cpu:>5.1f}%\n"
        f"RAM     {ram.percent:>5.1f}%   {ram.used // 1024**2} MB / {ram.total // 1024**2} MB\n"
        f"Disk    {disk.percent:>5.1f}%   {disk.used // 1024**3} GB / {disk.total // 1024**3} GB\n"
        f"Uptime  {days}d {hours}h {minutes}m"
    )
    return "🖥  <b>Server</b>\n\n" + terminal_block("uptime && free -h && df -h", body)


# ============================================================
# PROCESS MANAGEMENT
# ============================================================

def start_project(user_id: int, project_name: str):
    meta = project_meta(user_id, project_name)
    if not meta:
        return False, "Project not found."

    pdir = project_dir(user_id, project_name)
    entry = meta.get("entry")
    entry_path = pdir / entry

    if not entry_path.exists():
        return False, "Entry file missing."

    venv_python = pdir / "venv" / "bin" / "python"
    python_bin = str(venv_python) if venv_python.exists() else "python3"

    log_path = log_file_path(user_id, project_name)

    with open(log_path, "a") as log_f:
        process = psutil.Popen(
            [python_bin, entry],
            cwd=str(pdir),
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
        )

    pid_file(user_id, project_name).write_text(str(process.pid))
    return True, f"Started with PID {process.pid}"


def stop_project(user_id: int, project_name: str):
    pid = get_pid(user_id, project_name)
    if pid is None:
        return False, "Not running."

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            return False, str(exc)

    pid_file(user_id, project_name).unlink(missing_ok=True)
    return True, "Stopped."


# ============================================================
# COMMAND RUNNER
# ============================================================

async def run_command(command, cwd=None, env=None):
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    output, _ = await process.communicate()
    return process.returncode, output.decode(errors="replace")


# ============================================================
# START / HELP / PROJECTS
# ============================================================

@dp.message(Command("start"))
async def start_handler(message: Message):
    if not allowed(message.from_user.id):
        return await access_denied(message)

    await message.answer(
        "🐍 <b>Python Hosting Panel</b>\n\n"
        "Send a <code>.py</code> file to create a project, "
        "then manage everything below.",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    if not allowed(message.from_user.id):
        return await access_denied(message)

    await message.answer(
        "🐍 <b>Help</b>\n\n"
        "1️⃣ Send a <code>.py</code> file\n"
        "2️⃣ Send <code>requirements.txt</code> if needed\n"
        "3️⃣ Create <code>.env</code> from the project menu\n"
        "4️⃣ Open the project → Start\n\n"
        "<b>Extras</b>\n"
        "📁 Files — browse and edit code, paginated\n"
        "🌱 .env — create/replace secrets by chat, no upload\n"
        "⬇️ Download — get a .zip of the whole project\n\n"
        "<b>Commands</b>\n"
        "/start  /projects  /server  /help",
        parse_mode="HTML",
    )


@dp.message(Command("projects"))
async def projects_command(message: Message):
    if not allowed(message.from_user.id):
        return await access_denied(message)

    projects = user_projects(message.from_user.id)
    if not projects:
        await message.answer(
            "📦 <b>No projects yet.</b>\nSend a <code>.py</code> file to create one.",
            parse_mode="HTML",
        )
        return

    await message.answer(
        "📦 <b>Your Projects</b>",
        reply_markup=project_list_keyboard(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(Command("server"))
async def server_command(message: Message):
    if not allowed(message.from_user.id):
        return await access_denied(message)
    await message.answer(await server_status_text(), parse_mode="HTML")


# ============================================================
# SIMPLE CALLBACKS
# ============================================================

@dp.callback_query(F.data == "home")
async def home_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)
    await callback.message.edit_text(
        "🐍 <b>Python Hosting Panel</b>",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def noop_cb(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "help")
async def help_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)
    await callback.message.answer(
        "🐍 <b>Help</b>\n\n"
        "Send a <code>.py</code> file to create a project, "
        "manage it from the Projects menu.",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "upload_help")
async def upload_help_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)
    await callback.message.answer(
        "📤 <b>Upload a project</b>\n\n"
        "Send your <code>.py</code> file directly.\n"
        "Then, if needed, send <code>requirements.txt</code>.\n\n"
        "For secrets, use 🌱 <b>.env</b> in the project menu — "
        "no file upload needed.",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "projects")
async def projects_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    projects = user_projects(callback.from_user.id)
    if not projects:
        await callback.message.edit_text(
            "📦 <b>No projects yet.</b>\nSend a <code>.py</code> file to create one.",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        "📦 <b>Your Projects</b>",
        reply_markup=project_list_keyboard(callback.from_user.id),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "server")
async def server_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)
    await callback.message.edit_text(
        await server_status_text(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="server")],
            [InlineKeyboardButton(text="⬅️  Home", callback_data="home")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "github_update")
async def github_update_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    status = await callback.message.answer(
        terminal_block("git pull", "Fetching latest changes..."),
        parse_mode="HTML",
    )
    code, output = await run_command(["git", "pull"], cwd=BASE_DIR)
    footer = "✅ Done — restart the bot to apply." if code == 0 else "❌ git pull failed."
    await status.edit_text(terminal_block("git pull", output.strip()[-2500:], footer), parse_mode="HTML")
    await callback.answer()


# ============================================================
# PROJECT OPEN / STATUS / LOGS / START / STOP / RESTART / DELETE
# ============================================================

@dp.callback_query(F.data.startswith("open:"))
async def open_project_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    name = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    meta = project_meta(user_id, name)

    if not meta:
        await callback.answer("Project not found.", show_alert=True)
        return

    running = is_running(user_id, name)
    dot = "🟢 Running" if running else "⚪️ Stopped"

    text = (
        f"📦 <b>{html_escape(name)}</b>\n\n"
        f"Status   {dot}\n"
        f"Entry    <code>{html_escape(meta.get('entry', '-'))}</code>\n"
        f"Created  <code>{meta.get('created', '-')[:19].replace('T', ' ')}</code>"
    )

    await callback.message.edit_text(
        text,
        reply_markup=project_keyboard(name, running),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("status:"))
async def status_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    name = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    pid = get_pid(user_id, name)

    if pid is None:
        body = "not running"
    else:
        try:
            proc = psutil.Process(pid)
            with proc.oneshot():
                cpu = proc.cpu_percent(interval=0.3)
                mem = proc.memory_info().rss // 1024**2
                started = datetime.fromtimestamp(proc.create_time()).strftime("%Y-%m-%d %H:%M:%S")
            body = f"pid      {pid}\ncpu      {cpu:.1f}%\nmem      {mem} MB\nstarted  {started}"
        except Exception as exc:
            body = f"error reading process: {exc}"

    await callback.message.answer(
        terminal_block(f"ps -p {pid or '-'} -o pid,%cpu,rss,etime", body),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("logs:"))
async def logs_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    _, name, page_raw = callback.data.split(":", 2)
    page = int(page_raw)
    user_id = callback.from_user.id
    log_path = log_file_path(user_id, name)

    if not log_path.exists():
        await callback.answer("No logs yet.", show_alert=True)
        return

    lines = log_path.read_text(errors="replace").splitlines()
    lines = lines[-500:]  # cap for performance
    chunk, page, total_pages, _ = paginate_lines(lines, page)
    body = "\n".join(chunk) if chunk else "(empty)"

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"logs:{name}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"logs:{name}:{page + 1}"))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="🔄 Refresh", callback_data=f"logs:{name}:{page}")],
        [InlineKeyboardButton(text="⬅️  Back", callback_data=f"open:{name}")],
    ])

    try:
        await callback.message.edit_text(
            terminal_block(f"tail -n {PAGE_SIZE} bot.log", body[-3500:]),
            reply_markup=kb,
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("start:"))
async def start_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    name = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    ok, msg = start_project(user_id, name)
    await callback.answer(msg, show_alert=not ok)

    meta = project_meta(user_id, name)
    running = is_running(user_id, name)
    dot = "🟢 Running" if running else "⚪️ Stopped"
    text = (
        f"📦 <b>{html_escape(name)}</b>\n\n"
        f"Status   {dot}\n"
        f"Entry    <code>{html_escape(meta.get('entry', '-'))}</code>"
    )
    await callback.message.edit_text(text, reply_markup=project_keyboard(name, running), parse_mode="HTML")


@dp.callback_query(F.data.startswith("stop:"))
async def stop_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    name = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    ok, msg = stop_project(user_id, name)
    await callback.answer(msg, show_alert=not ok)

    meta = project_meta(user_id, name)
    running = is_running(user_id, name)
    dot = "🟢 Running" if running else "⚪️ Stopped"
    text = (
        f"📦 <b>{html_escape(name)}</b>\n\n"
        f"Status   {dot}\n"
        f"Entry    <code>{html_escape(meta.get('entry', '-'))}</code>"
    )
    await callback.message.edit_text(text, reply_markup=project_keyboard(name, running), parse_mode="HTML")


@dp.callback_query(F.data.startswith("restart:"))
async def restart_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    name = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    stop_project(user_id, name)
    await asyncio.sleep(0.5)
    ok, msg = start_project(user_id, name)
    await callback.answer(f"Restarted — {msg}" if ok else msg, show_alert=not ok)

    meta = project_meta(user_id, name)
    running = is_running(user_id, name)
    dot = "🟢 Running" if running else "⚪️ Stopped"
    text = (
        f"📦 <b>{html_escape(name)}</b>\n\n"
        f"Status   {dot}\n"
        f"Entry    <code>{html_escape(meta.get('entry', '-'))}</code>"
    )
    await callback.message.edit_text(text, reply_markup=project_keyboard(name, running), parse_mode="HTML")


@dp.callback_query(F.data.startswith("askdelete:"))
async def ask_delete_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)
    name = callback.data.split(":", 1)[1]
    await callback.message.edit_text(
        f"🗑  Delete <b>{html_escape(name)}</b>? This removes all its files.",
        reply_markup=confirm_delete_keyboard(name),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("dodelete:"))
async def do_delete_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    name = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    stop_project(user_id, name)
    shutil.rmtree(project_dir(user_id, name), ignore_errors=True)
    user_projects(user_id).pop(name, None)
    save_db(db)

    await callback.message.edit_text(f"🗑  Deleted <b>{html_escape(name)}</b>.", parse_mode="HTML")
    await callback.answer("Deleted")


# ============================================================
# FILE BROWSER / PAGINATED VIEWER / LINE EDIT / DOWNLOAD
# ============================================================

@dp.callback_query(F.data.startswith("files:"))
async def files_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    name = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    files = list_project_files(user_id, name)

    if not files:
        await callback.answer("No files found.", show_alert=True)
        return

    await callback.message.edit_text(
        f"📁 <b>{html_escape(name)}</b> — files",
        reply_markup=file_list_keyboard(name, files),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("vfile:"))
async def view_file_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    _, name, idx_raw, page_raw = callback.data.split(":", 3)
    idx = int(idx_raw)
    page = int(page_raw)
    user_id = callback.from_user.id

    files = list_project_files(user_id, name)
    if idx >= len(files):
        await callback.answer("File not found.", show_alert=True)
        return

    rel_path = files[idx]
    full_path = project_dir(user_id, name) / rel_path

    try:
        raw_lines = full_path.read_text(errors="replace").splitlines()
    except Exception as exc:
        await callback.answer(f"Can't read file: {exc}", show_alert=True)
        return

    if rel_path == ".env":
        raw_lines = [mask_env_line(l) for l in raw_lines]

    chunk, page, total_pages, start = paginate_lines(raw_lines, page)
    numbered = "\n".join(f"{start + i + 1:>4} │ {line}" for i, line in enumerate(chunk))

    text = f"📄 <b>{html_escape(rel_path)}</b>\n\n" + terminal_block(
        f"sed -n '{start + 1},{start + len(chunk)}p' {rel_path}",
        numbered or "(empty file)",
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=file_view_keyboard(name, idx, page, total_pages),
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("dlfile:"))
async def download_file_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    _, name, idx_raw = callback.data.split(":", 2)
    idx = int(idx_raw)
    user_id = callback.from_user.id
    files = list_project_files(user_id, name)

    if idx >= len(files):
        await callback.answer("File not found.", show_alert=True)
        return

    full_path = project_dir(user_id, name) / files[idx]
    await callback.message.answer_document(FSInputFile(full_path))
    await callback.answer()


@dp.callback_query(F.data.startswith("zip:"))
async def zip_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    name = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    pdir = project_dir(user_id, name)

    if not pdir.exists():
        await callback.answer("Project not found.", show_alert=True)
        return

    await callback.answer("Zipping...")
    zip_path = TMP_DIR / f"{name}_{user_id}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, filenames in os.walk(pdir):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
            for fname in filenames:
                if fname in IGNORED_FILE_NAMES:
                    continue
                full = Path(root) / fname
                zf.write(full, arcname=full.relative_to(pdir))

    await callback.message.answer_document(
        FSInputFile(zip_path, filename=f"{name}.zip"),
        caption=f"📦 {name}",
    )
    zip_path.unlink(missing_ok=True)


# ============================================================
# EDIT LINES (FSM)
# ============================================================

@dp.callback_query(F.data.startswith("editlines:"))
async def edit_lines_start_cb(callback: CallbackQuery, state: FSMContext):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    _, name, idx_raw = callback.data.split(":", 2)
    idx = int(idx_raw)
    user_id = callback.from_user.id
    files = list_project_files(user_id, name)

    if idx >= len(files):
        await callback.answer("File not found.", show_alert=True)
        return

    rel_path = files[idx]
    total_lines = len((project_dir(user_id, name) / rel_path).read_text(errors="replace").splitlines())

    await state.update_data(project=name, rel_path=rel_path)
    await state.set_state(EditStates.waiting_range)

    await callback.message.answer(
        f"✏️ Editing <code>{html_escape(rel_path)}</code> ({total_lines} lines)\n\n"
        "Send the line range to replace, e.g. <code>10-15</code> "
        "(or a single line like <code>7</code>).",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(EditStates.waiting_range)
async def edit_lines_range_msg(message: Message, state: FSMContext):
    if not allowed(message.from_user.id):
        return await access_denied(message)

    text = message.text.strip()
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", text)

    if not match:
        await message.answer("Format not recognised. Send like <code>10-15</code> or <code>7</code>.", parse_mode="HTML")
        return

    start_line = int(match.group(1))
    end_line = int(match.group(2)) if match.group(2) else start_line

    if start_line < 1 or end_line < start_line:
        await message.answer("Invalid range.")
        return

    await state.update_data(start_line=start_line, end_line=end_line)
    await state.set_state(EditStates.waiting_content)

    await message.answer(
        f"Now send the replacement content for lines <code>{start_line}-{end_line}</code>.\n"
        "Send <code>/blank</code> to delete those lines instead.",
        parse_mode="HTML",
    )


@dp.message(EditStates.waiting_content)
async def edit_lines_content_msg(message: Message, state: FSMContext):
    if not allowed(message.from_user.id):
        return await access_denied(message)

    data = await state.get_data()
    name = data["project"]
    rel_path = data["rel_path"]
    start_line = data["start_line"]
    end_line = data["end_line"]
    user_id = message.from_user.id

    full_path = project_dir(user_id, name) / rel_path
    lines = full_path.read_text(errors="replace").splitlines()

    new_content = "" if message.text.strip() == "/blank" else message.text
    new_lines = new_content.split("\n") if new_content else []

    if end_line > len(lines):
        end_line = len(lines)

    updated = lines[:start_line - 1] + new_lines + lines[end_line:]
    full_path.write_text("\n".join(updated) + "\n", encoding="utf-8")

    await state.clear()

    await message.answer(
        f"✅ Updated <code>{html_escape(rel_path)}</code>, lines "
        f"{start_line}-{end_line} → {len(new_lines)} new line(s).\n\n"
        "Restart the project for changes to take effect.",
        parse_mode="HTML",
    )


# ============================================================
# .ENV — CREATE VIA CHAT (NO UPLOAD)
# ============================================================

@dp.callback_query(F.data.startswith("envmenu:"))
async def env_menu_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)
    name = callback.data.split(":", 1)[1]
    await callback.message.edit_text(
        f"🌱 <b>.env</b> — {html_escape(name)}",
        reply_markup=env_menu_keyboard(name),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("envnew:"))
async def env_new_cb(callback: CallbackQuery, state: FSMContext):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    name = callback.data.split(":", 1)[1]
    await state.update_data(project=name)
    await state.set_state(EnvStates.waiting_content)

    await callback.message.answer(
        "✍️ Send the full <code>.env</code> content, one <code>KEY=VALUE</code> per line, "
        "e.g.:\n<pre>BOT_TOKEN=123:abc\nADMIN_ID=123456789</pre>\n"
        "This replaces the file directly — no upload needed.",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(EnvStates.waiting_content)
async def env_new_msg(message: Message, state: FSMContext):
    if not allowed(message.from_user.id):
        return await access_denied(message)

    data = await state.get_data()
    name = data["project"]
    user_id = message.from_user.id

    pdir = project_dir(user_id, name)
    pdir.mkdir(parents=True, exist_ok=True)
    env_path = pdir / ".env"
    env_path.write_text(message.text.strip() + "\n", encoding="utf-8")

    meta = project_meta(user_id, name)
    if meta is not None:
        meta["has_env"] = True
        save_db(db)

    await state.clear()

    lines = message.text.strip().splitlines()
    masked = "\n".join(mask_env_line(l) for l in lines)

    await message.answer(
        f"✅ <code>.env</code> saved for <b>{html_escape(name)}</b>\n\n"
        + terminal_block("cat .env", masked),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("envview:"))
async def env_view_cb(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return await access_denied_cb(callback)

    name = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    env_path = project_dir(user_id, name) / ".env"

    if not env_path.exists():
        await callback.answer("No .env file yet.", show_alert=True)
        return

    lines = env_path.read_text(errors="replace").splitlines()
    masked = "\n".join(mask_env_line(l) for l in lines) or "(empty)"

    await callback.message.answer(terminal_block("cat .env", masked), parse_mode="HTML")
    await callback.answer()


# ============================================================
# DOCUMENT UPLOAD (.py / requirements.txt)
# ============================================================

@dp.message(F.document)
async def document_handler(message: Message):
    if not allowed(message.from_user.id):
        return await access_denied(message)

    document = message.document
    filename = document.file_name or ""
    lower = filename.lower()
    user_id = message.from_user.id
    projects = user_projects(user_id)

    # ------------------------------------------------------
    # PYTHON FILE
    # ------------------------------------------------------
    if lower.endswith(".py"):
        project_name = safe_name(filename)

        if project_name in projects:
            await message.answer(
                f"⚠️ Project <code>{project_name}</code> already exists. "
                "Delete it first or rename the file.",
                parse_mode="HTML",
            )
            return

        pdir = project_dir(user_id, project_name)
        pdir.mkdir(parents=True, exist_ok=True)
        py_path = pdir / Path(filename).name

        status_message = await message.answer(
            terminal_block(f"curl -O {filename}", "Downloading..."),
            parse_mode="HTML",
        )

        try:
            file = await bot.get_file(document.file_id)
            await bot.download_file(file.file_path, destination=py_path)
        except Exception as e:
            await status_message.edit_text(
                terminal_block(f"curl -O {filename}", str(e), "❌ Download failed."),
                parse_mode="HTML",
            )
            return

        projects[project_name] = {
            "entry": py_path.name,
            "created": datetime.now().isoformat(),
            "requirements": False,
        }
        save_db(db)

        await status_message.edit_text(
            f"✅ <b>Project created</b> — <code>{html_escape(project_name)}</code>\n\n"
            f"Entry: <code>{html_escape(py_path.name)}</code>\n\n"
            "Send <code>requirements.txt</code> if needed, set up 🌱 .env, then ▶️ Start.",
            reply_markup=project_keyboard(project_name, False),
            parse_mode="HTML",
        )
        return

    # ------------------------------------------------------
    # REQUIREMENTS
    # ------------------------------------------------------
    if lower == "requirements.txt":
        if not projects:
            await message.answer("❌ Create a Python project first by sending a .py file.")
            return

        if len(projects) != 1:
            await message.answer(
                "⚠️ Multiple projects exist. Open the target project first, "
                "then send requirements.txt right after."
            )
            return

        project_name = list(projects.keys())[0]
        pdir = project_dir(user_id, project_name)
        requirements_path = pdir / "requirements.txt"

        file = await bot.get_file(document.file_id)
        await bot.download_file(file.file_path, destination=requirements_path)

        projects[project_name]["requirements"] = True
        save_db(db)

        await install_requirements_live(message, user_id, project_name, requirements_path)
        return

    # ------------------------------------------------------
    # .ENV UPLOAD — DISABLED, POINT TO CHAT-BASED FLOW
    # ------------------------------------------------------
    if lower == ".env":
        await message.answer(
            "⚠️ <code>.env</code> upload is disabled for safety.\n\n"
            "Open your project → 🌱 <b>.env</b> → ✍️ Create/Replace, "
            "and paste the content in chat instead.",
            parse_mode="HTML",
        )
        return

    # ------------------------------------------------------
    # UNKNOWN
    # ------------------------------------------------------
    await message.answer(
        "❌ Unsupported file.\n\nSupported: <code>.py</code>, <code>requirements.txt</code>",
        parse_mode="HTML",
    )


# ============================================================
# LIVE REQUIREMENTS INSTALL — TERMINAL FEEL
# ============================================================

async def install_requirements_live(message: Message, user_id: int, project_name: str, requirements_path: Path):
    pdir = project_dir(user_id, project_name)
    venv_dir = pdir / "venv"

    status = await message.answer(
        terminal_block(f"python3 -m venv venv", "creating virtual environment..."),
        parse_mode="HTML",
    )

    if not venv_dir.exists():
        code, output = await run_command(["python3", "-m", "venv", "venv"], cwd=pdir)
        if code != 0:
            await status.edit_text(terminal_block("python3 -m venv venv", output[-2000:], "❌ venv failed."), parse_mode="HTML")
            return

    pip_bin = venv_dir / "bin" / "pip"
    start_time = time.time()

    process = await asyncio.create_subprocess_exec(
        str(pip_bin), "install", "-r", str(requirements_path), "--progress-bar", "off",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(pdir),
    )

    output_lines = []
    last_update = 0
    tick = 0

    while True:
        line = await process.stdout.readline()
        if not line:
            break

        decoded = line.decode(errors="replace").strip()
        if decoded:
            output_lines.append(decoded)
            output_lines = output_lines[-14:]

        now = time.time()
        if now - last_update >= 1.5:
            last_update = now
            tick += 1
            elapsed = round(now - start_time, 1)
            recent = "\n".join(output_lines[-10:])[-2500:]

            text = (
                f"{spinner_frame(tick)} <b>Installing requirements</b> — "
                f"<code>{html_escape(project_name)}</code>  ({elapsed}s)\n\n"
                + terminal_block("pip install -r requirements.txt", recent or "starting...")
            )

            try:
                await status.edit_text(text, parse_mode="HTML")
            except TelegramBadRequest:
                pass

    returncode = await process.wait()
    elapsed = round(time.time() - start_time, 1)
    recent = "\n".join(output_lines[-10:])[-2500:]

    if returncode == 0:
        footer = f"✅ Done in {elapsed}s"
    else:
        footer = f"❌ pip exited with code {returncode}"

    await status.edit_text(
        f"<b>{html_escape(project_name)}</b>\n\n"
        + terminal_block("pip install -r requirements.txt", recent, footer),
        reply_markup=project_keyboard(project_name, is_running(user_id, project_name)),
        parse_mode="HTML",
    )


# ============================================================
# ENTRYPOINT
# ============================================================

async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
