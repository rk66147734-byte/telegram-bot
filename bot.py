"""Small private Telegram bot for preventing duplicate Facebook client claims."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

BOT_TOKEN = "8934451968:AAEZ_w598BsHL17JgPkxmjIosu5_lxuOLKk"

JOIN_CODE = "myteam2026"

try:
    from dotenv import load_dotenv
    from telegram import ReplyKeyboardMarkup, Update
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
    DEPENDENCIES_AVAILABLE = True
except ModuleNotFoundError:  # Lets the pure SQLite tests run before package installation.
    DEPENDENCIES_AVAILABLE = False
    Update = object
    Application = CommandHandler = ContextTypes = MessageHandler = filters = ReplyKeyboardMarkup = None

    def load_dotenv(*_args, **_kwargs):
        return False

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "claims.db"
# New users must send this code with /join <code> to register.
# Change this value to your own private code before deployment.
JOIN_CODE = "CHANGE_THIS_JOIN_CODE"
NEW_CLAIM = "➕ নতুন Client"
SEARCH = "🔍 খুঁজুন"
HISTORY = "📜 History"
HELP = "❓ সাহায্য"
SAVE_CLAIM = "✅ Save Client"
CANCEL_CLAIM = "✖ বাতিল"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_id_list(value: str) -> set[int]:
    result = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            result.add(int(item))
    return result


def normalise_facebook_target(raw: str) -> str:
    """Turn a Facebook URL, username, or numeric ID into a comparable key."""
    value = raw.strip().lower().rstrip("/")
    if not value:
        raise ValueError("Please provide a Facebook URL, username, or numeric ID.")

    # A plain username or page/profile numeric ID is accepted.
    if re.fullmatch(r"[a-z0-9._-]+", value):
        return value

    if not re.match(r"^https?://", value):
        value = "https://" + value
    parsed = urlparse(value)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host not in {"facebook.com", "fb.com"}:
        raise ValueError("That does not look like a Facebook URL.")

    path = parsed.path.strip("/").lower()
    if not path:
        raise ValueError("The Facebook URL needs a page, profile, username, or ID.")
    # profile.php?id=123, pages/name/123, and standard /username paths.
    query = parsed.query
    match = re.search(r"(?:^|&)id=([0-9]+)(?:&|$)", query)
    if path == "profile.php" and match:
        return match.group(1)
    parts = [part for part in path.split("/") if part]
    if parts[0] == "pages" and len(parts) >= 3:
        return parts[-1]
    return parts[0]


@dataclass(frozen=True)
class Settings:
    token: str
    members: set[int]
    admins: set[int]
    allowed_chat_id: int | None
    duplicate_message: str


class ClaimStore:
    def __init__(self, path: Path):
        self.path = path
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def session(self):
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.session() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS members (
                    user_id INTEGER PRIMARY KEY,
                    user_name TEXT NOT NULL,
                    joined_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    facebook_key TEXT NOT NULL,
                    original_input TEXT NOT NULL,
                    claimed_by_id INTEGER NOT NULL,
                    claimed_by_name TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    profile_name TEXT,
                    screenshot_file_id TEXT,
                    released_at TEXT,
                    released_by_id INTEGER,
                    released_by_name TEXT
                )
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(claims)")}
            if "profile_name" not in columns:
                db.execute("ALTER TABLE claims ADD COLUMN profile_name TEXT")
            if "screenshot_file_id" not in columns:
                db.execute("ALTER TABLE claims ADD COLUMN screenshot_file_id TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_claim_key ON claims(facebook_key) WHERE released_at IS NULL")

    def add_member(self, user_id: int, name: str) -> None:
        with self.session() as db:
            db.execute(
                """INSERT INTO members (user_id, user_name, joined_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET user_name = excluded.user_name""",
                (user_id, name, utc_now()),
            )

    def is_member(self, user_id: int) -> bool:
        with self.session() as db:
            return db.execute(
                "SELECT 1 FROM members WHERE user_id = ?",
                (user_id,),
            ).fetchone() is not None

    def claim(self, key: str, original: str, user_id: int, name: str, profile_name: str = "", screenshot_file_id: str = "") -> tuple[bool, sqlite3.Row]:
        try:
            with self.session() as db:
                cursor = db.execute(
                    """INSERT INTO claims
                    (facebook_key, original_input, claimed_by_id, claimed_by_name, claimed_at, profile_name, screenshot_file_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (key, original, user_id, name, utc_now(), profile_name, screenshot_file_id),
                )
                return True, db.execute("SELECT * FROM claims WHERE id = ?", (cursor.lastrowid,)).fetchone()
        except sqlite3.IntegrityError:
            with self.session() as db:
                return False, db.execute("SELECT * FROM claims WHERE facebook_key = ? AND released_at IS NULL", (key,)).fetchone()

    def search(self, term: str) -> list[sqlite3.Row]:
        term = term.strip().lower()
        try:
            facebook_key = normalise_facebook_target(term)
        except ValueError:
            facebook_key = ""
        with self.session() as db:
            return db.execute("""
                SELECT * FROM claims
                WHERE facebook_key = ? OR facebook_key LIKE ? OR original_input LIKE ? OR profile_name LIKE ?
                ORDER BY CASE WHEN released_at IS NULL THEN 0 ELSE 1 END, claimed_at DESC LIMIT 10
            """, (facebook_key, f"%{term}%", f"%{term}%", f"%{term}%")).fetchall()

    def recent_for_user(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        with self.session() as db:
            return db.execute(
                "SELECT * FROM claims WHERE claimed_by_id = ? ORDER BY claimed_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()

    def recent_all(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.session() as db:
            return db.execute(
                "SELECT * FROM claims ORDER BY claimed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def release(self, claim_id: int, admin_id: int, admin_name: str) -> bool:
        with self.session() as db:
            result = db.execute("""
                UPDATE claims SET released_at = ?, released_by_id = ?, released_by_name = ?
                WHERE id = ? AND released_at IS NULL
            """, (utc_now(), admin_id, admin_name, claim_id))
            return result.rowcount == 1

def display_name(update: Update) -> str:
    user = update.effective_user
    return user.full_name or user.username or str(user.id)


def row_text(row: sqlite3.Row) -> str:
    status = "🟢 Active / চালু" if not row["released_at"] else "🟡 Released / ছেড়ে দেওয়া হয়েছে"
    profile_name = f"\n👤 Client name / ক্লায়েন্ট: {row['profile_name']}" if row["profile_name"] else ""
    screenshot = "\n🖼️ Screenshot / ছবি: Saved / সংরক্ষিত" if row["screenshot_file_id"] else ""
    link_match = re.search(r"(?:https?://)?(?:www\.)?(?:facebook\.com|fb\.com)/\S+", row["original_input"], re.I)
    facebook_link = f"\n🔗 Facebook link: {link_match.group(0)}" if link_match else ""
    return f"🧾 Claim #{row['id']} | {status}{profile_name}{facebook_link}{screenshot}\n🙋 Claimed by / যিনি নিয়েছেন: {row['claimed_by_name']}\n🕐 সময়: {row['claimed_at']}"

def search_result_text(row: sqlite3.Row) -> str:
    """Compact global-search result: enough to prevent duplicate claims without exposing a history list."""
    status = "🟢 Active / চালু" if not row["released_at"] else "🟡 Released / ছেড়ে দেওয়া হয়েছে"
    profile_name = f"\n👤 Client name / ক্লায়েন্ট: {row['profile_name']}" if row["profile_name"] else ""
    link_match = re.search(r"(?:https?://)?(?:www\.)?(?:facebook\.com|fb\.com)/\S+", row["original_input"], re.I)
    facebook_link = f"\n🔗 Facebook link: {link_match.group(0)}" if link_match else ""
    return f"🧾 Claim #{row['id']} | {status}{profile_name}{facebook_link}\n🙋 Claimed by / যিনি নিয়েছেন: {row['claimed_by_name']}\n🕐 Claim time: {row['claimed_at']}"


def duplicate_reply(template: str, row: sqlite3.Row) -> str:
    """Let the owner customise the duplicate warning without exposing internal keys."""
    try:
        return template.format(
            claimed_by=row["claimed_by_name"],
            client_name=row["profile_name"] or "this client",
            link=row["original_input"],
        )
    except (KeyError, ValueError):
        return f"This client was already claimed by {row['claimed_by_name']}. Please choose another client."


def authorized(settings: Settings, update: Update, admin: bool = False) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    if settings.allowed_chat_id is not None and chat.id != settings.allowed_chat_id:
        return False
    return user.id in settings.admins if admin else True


def require_member(context: ContextTypes.DEFAULT_TYPE, update: Update) -> bool:
    return context.application.bot_data["store"].is_member(update.effective_user.id)

def main_menu():
    return ReplyKeyboardMarkup(
        [[NEW_CLAIM, SEARCH], [HISTORY, HELP]],
        resize_keyboard=True,
        input_field_placeholder="নিচের একটি button চাপুন",
    )


def claim_menu():
    return ReplyKeyboardMarkup(
        [[SAVE_CLAIM, CANCEL_CLAIM], [NEW_CLAIM, SEARCH], [HISTORY, HELP]],
        resize_keyboard=True,
        input_field_placeholder="তথ্য পাঠান অথবা Save Client চাপুন",
    )


async def reject(update: Update, text: str = "You are not authorised to use this command.") -> None:
    await update.effective_message.reply_text(text)


def command_arg(update: Update) -> str:
    text = update.effective_message.text or update.effective_message.caption or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 and parts[0].startswith("/") else text.strip()


def clean_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def claim_details(raw: str, screenshot_file_id: str = "") -> tuple[str, str, str]:
    """Return a duplicate-check key, a profile name, and original text."""
    original = raw.strip()
    name_match = re.search(r"(?:^|\n)\s*name\s*:\s*(.+?)(?=\n|$)", original, re.I)
    link_match = re.search(r"(?:https?://)?(?:www\.)?(?:facebook\.com|fb\.com)/\S+", original, re.I)
    profile_name = clean_name(name_match.group(1)) if name_match else ""
    if link_match:
        return normalise_facebook_target(link_match.group(0)), profile_name, original
    if profile_name:
        return "name:" + re.sub(r"[^a-z0-9]+", "-", profile_name.lower()).strip("-"), profile_name, original
    if original:
        # A one-word entry is treated as a Facebook username/ID; a multi-word entry as a name.
        if " " not in original and "\n" not in original:
            return normalise_facebook_target(original), "", original
        profile_name = clean_name(original)
        return "name:" + re.sub(r"[^a-z0-9]+", "-", profile_name.lower()).strip("-"), profile_name, original
    if screenshot_file_id:
        return "image:" + screenshot_file_id, "", "Screenshot only"
    raise ValueError("Send a Facebook link, username/ID, name, or a screenshot.")


def extract_client_fields(text: str) -> tuple[str, str]:
    """Find a Facebook link and the remaining text as a possible client name."""
    link_match = re.search(r"(?:https?://)?(?:www\.)?(?:facebook\.com|fb\.com)/\S+", text, re.I)
    link = link_match.group(0) if link_match else ""
    remaining = text.replace(link, "") if link else text
    remaining = re.sub(r"(?im)(?:^|\n)\s*(?:name|client\s*name|নাম|link|profile\s*link)\s*[:=-]?\s*", "\n", remaining)
    return clean_name(remaining.strip(" \n,;|-")), link


async def show_draft_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data["draft"]
    name = draft.get("name") or "এখনও দেওয়া হয়নি"
    link = draft.get("link") or "এখনও দেওয়া হয়নি"
    screenshot = "আছে" if draft.get("screenshot") else "নেই"
    await update.effective_message.reply_text(
        f"এখন পর্যন্ত:\nনাম: {name}\nLink: {link}\nScreenshot: {screenshot}\n\nআরও তথ্য পাঠান, অথবা ✅ Save Client চাপুন।",
        reply_markup=claim_menu(),
    )


async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    code = command_arg(update).strip()
    if not code:
        await update.effective_message.reply_text("ব্যবহার: /join <join code>")
        return

    if code != JOIN_CODE:
        await update.effective_message.reply_text("❌ Join code ভুল। সঠিক code দিয়ে আবার চেষ্টা করুন।")
        return

    store: ClaimStore = context.application.bot_data["store"]
    store.add_member(user.id, display_name(update))
    await update.effective_message.reply_text(
        "✅ আপনি সফলভাবে team-এ add হয়েছেন। এখন /start চাপুন।",
        reply_markup=main_menu(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: ClaimStore = context.application.bot_data["store"]
    if not store.is_member(update.effective_user.id):
        await update.effective_message.reply_text(
            "আপনি এখনও team-এ registered নন।\n"
            "Admin-এর দেওয়া join code দিয়ে লিখুন:\n"
            "/join <join code>"
        )
        return
    await update.effective_message.reply_text(
        "Ready! নিচের button চাপুন। তারপর bot যা চাইবে শুধু সেটি পাঠান।",
        reply_markup=main_menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not require_member(context, update):
        await reject(update, "আপনি registered নন। আগে /join <join code> ব্যবহার করুন।")
        return
    extra = "\nAdmins: /release <claim number>" if update.effective_user.id in settings.admins else ""
    await update.effective_message.reply_text("➕ নতুন Client চাপুন, তারপর নাম/link লিখুন বা screenshot পাঠান।\n🔍 খুঁজুন চাপুন, তারপর client-এর নাম, Facebook ID বা link লিখুন। Search পুরো database-এ duplicate check করবে।\n📜 History চাপুন: প্রত্যেক member শুধু নিজের claim history দেখবে; admin সব claim দেখতে পারবে." + extra, reply_markup=main_menu())


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"Your Telegram user ID: {update.effective_user.id}\nName: {display_name(update)}")


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"This chat ID: {update.effective_chat.id}")


async def claim_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_claim(update, context)


async def photo_claim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("waiting_for") == "collect_claim":
        draft = context.user_data["draft"]
        draft["screenshot"] = update.effective_message.photo[-1].file_id
        name, link = extract_client_fields(update.effective_message.caption or "")
        if name:
            draft["name"] = name
        if link:
            draft["link"] = link
        await show_draft_status(update, context)
        return
    await save_claim(update, context, update.effective_message.photo[-1].file_id)


async def save_claim(update: Update, context: ContextTypes.DEFAULT_TYPE, screenshot_file_id: str = "", original: str | None = None) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not require_member(context, update):
        await reject(update, "আপনি registered নন। আগে /join <join code> ব্যবহার করুন।")
        return
    original = command_arg(update) if original is None else original
    try:
        key, profile_name, original = claim_details(original, screenshot_file_id)
    except ValueError as error:
        await update.effective_message.reply_text(f"{error}\nExample: /claim Rahim Ahmed\nOr: /claim https://facebook.com/example.page")
        return
    store: ClaimStore = context.application.bot_data["store"]
    created, row = store.claim(key, original, update.effective_user.id, display_name(update), profile_name, screenshot_file_id)
    if created:
        await update.effective_message.reply_text(f"Claim saved.\n{row_text(row)}")
    else:
        await update.effective_message.reply_text(f"{duplicate_reply(settings.duplicate_message, row)}\n\n{row_text(row)}")
    context.user_data.pop("waiting_for", None)
    context.user_data.pop("draft", None)


async def button_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Button-driven flow so teammates do not need to remember slash commands."""
    settings: Settings = context.application.bot_data["settings"]
    if not require_member(context, update):
        await reject(update, "আপনি registered নন। আগে /join <join code> ব্যবহার করুন।")
        return
    text = update.effective_message.text.strip()
    if text == NEW_CLAIM:
        context.user_data["waiting_for"] = "collect_claim"
        context.user_data["draft"] = {"name": "", "link": "", "screenshot": ""}
        await update.effective_message.reply_text(
            "এখন নাম, Facebook link বা screenshot—যেটা আছে পাঠান। একবারে সব দিতে হবে না। সব দেওয়া হলে ✅ Save Client চাপুন।",
            reply_markup=claim_menu(),
        )
        return
    if text == CANCEL_CLAIM:
        context.user_data.pop("waiting_for", None)
        context.user_data.pop("draft", None)
        await update.effective_message.reply_text("নতুন client যোগ করা বাতিল হয়েছে।", reply_markup=main_menu())
        return
    if text == SAVE_CLAIM:
        draft = context.user_data.get("draft")
        if not draft:
            await update.effective_message.reply_text("আগে ➕ নতুন Client চাপুন।", reply_markup=main_menu())
            return
        original = "\n".join(part for part in (f"Name: {draft['name']}" if draft["name"] else "", draft["link"]) if part)
        await save_claim(update, context, draft["screenshot"], original)
        return
    if text == SEARCH:
        context.user_data["waiting_for"] = "search"
        await update.effective_message.reply_text("এখন client-এর নাম বা Facebook link লিখে Send করুন।", reply_markup=main_menu())
        return
    if text == HISTORY:
        await history_command(update, context)
        return
    if text == HELP:
        await help_command(update, context)
        return
    waiting_for = context.user_data.get("waiting_for")
    if waiting_for == "collect_claim":
        draft = context.user_data["draft"]
        name, link = extract_client_fields(text)
        if name:
            draft["name"] = name
        if link:
            draft["link"] = link
        await show_draft_status(update, context)
        return
    if waiting_for == "search":
        rows = context.application.bot_data["store"].search(text.lower())
        await update.effective_message.reply_text("কোনো matching client পাওয়া যায়নি।" if not rows else "🔍 Search result / খোঁজার ফল\n\n" + "\n\n".join(search_result_text(row) for row in rows), reply_markup=main_menu())
        context.user_data.pop("waiting_for", None)
        return
    await update.effective_message.reply_text("নিচের button থেকে একটি বেছে নিন।", reply_markup=main_menu())


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not require_member(context, update):
        await reject(update, "আপনি registered নন। আগে /join <join code> ব্যবহার করুন।")
        return
    term = command_arg(update)
    if not term:
        await update.effective_message.reply_text("Usage: /search <username, ID, or part of a Facebook URL>")
        return
    rows = context.application.bot_data["store"].search(term.lower())
    await update.effective_message.reply_text("কোনো matching client পাওয়া যায়নি।" if not rows else "🔍 Search result / খোঁজার ফল\n\n" + "\n\n".join(search_result_text(row) for row in rows))


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not require_member(context, update):
        await reject(update, "আপনি registered নন। আগে /join <join code> ব্যবহার করুন।")
        return

    store: ClaimStore = context.application.bot_data["store"]
    is_admin = update.effective_user.id in settings.admins

    # Admins may review the team history; regular members can only see their own.
    rows = store.recent_all(50) if is_admin else store.recent_for_user(update.effective_user.id, 20)

    if not rows:
        title = "📜 All History / সব History" if is_admin else "📜 My History / আমার claim history"
        await update.effective_message.reply_text(
            f"{title}\n\nকোনো claim পাওয়া যায়নি।",
            reply_markup=main_menu(),
        )
        return

    title = "📜 All History / সব History" if is_admin else "📜 My History / আমার claim history"
    await update.effective_message.reply_text(
        title + "\n\n" + "\n\n".join(row_text(row) for row in rows),
        reply_markup=main_menu(),
    )


async def release_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not authorized(settings, update, admin=True):
        await reject(update, "Only configured admins can release claims.")
        return
    try:
        claim_id = int(command_arg(update))
    except ValueError:
        await update.effective_message.reply_text("Usage: /release <claim number> (for example, /release 12)")
        return
    changed = context.application.bot_data["store"].release(claim_id, update.effective_user.id, display_name(update))
    await update.effective_message.reply_text("Claim released; its history was kept." if changed else "Active claim not found.")


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("8934451968:AAEZ_w598BsHL17JgPkxmjIosu5_lxuOLKk", "").strip() or BOT_TOKEN
    members = parse_id_list(os.getenv("TEAM_MEMBER_IDS", ""))
    admins = parse_id_list(os.getenv("7097197639", ""))
    chat_id = os.getenv("ALLOWED_CHAT_ID", "").strip()
    duplicate_message = os.getenv(
        "DUPLICATE_MESSAGE",
        "🚫 Duplicate Client Alert / ডুপ্লিকেট ক্লায়েন্ট সতর্কতা\n\nএই client-টি আগে থেকেই {claimed_by}-এর নেওয়া আছে।\nThis client is already claimed by {claimed_by}.\n\n✨ দয়া করে নতুন client দিন / Please choose another client.",
    ).strip()
    if not token or token == "put_your_botfather_token_here":
        raise RuntimeError("Set 8934451968:AAEZ_w598BsHL17JgPkxmjIosu5_lxuOLKk in your local .env file.")
    # TEAM_MEMBER_IDS is kept for backward compatibility, but new members are stored in claims.db.
    return Settings(token, members, admins, int(chat_id) if chat_id else None, duplicate_message)


def main() -> None:
    if not DEPENDENCIES_AVAILABLE:
        raise RuntimeError("Install the packages first: py -m pip install -r requirements.txt")
    settings = load_settings()
    store = ClaimStore(DB_PATH)
    app = Application.builder().token(settings.token).build()
    app.bot_data.update(settings=settings, store=store)
    app.add_handler(CommandHandler("join", join_command))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("chatid", chatid))
    # Put photo handling before /claim so a captioned photo keeps its screenshot ID too.
    app.add_handler(MessageHandler(filters.PHOTO, photo_claim))
    app.add_handler(CommandHandler("claim", claim_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("release", release_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_input))
    logging.info("Bot is starting. Press Ctrl+C to stop.")
    # Python 3.14 no longer creates an event loop automatically in the main thread.
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Prevent request URLs (which contain the bot token) from being printed in the terminal.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    main()
