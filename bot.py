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

TEAM_JOIN_CODE = "myteam2026"
ADMIN_JOIN_CODE = "km195770"

BOT_TOKEN = "8934451968:AAEZ_w598BsHL17JgPkxmjIosu5_lxuOLKk"
ADMIN_USER_IDS = ""
try:
    from dotenv import load_dotenv
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
    DEPENDENCIES_AVAILABLE = True
except ModuleNotFoundError:  # Lets the pure SQLite tests run before package installation.
    DEPENDENCIES_AVAILABLE = False
    Update = object
    Application = CallbackQueryHandler = CommandHandler = ContextTypes = MessageHandler = filters = ReplyKeyboardMarkup = InlineKeyboardButton = InlineKeyboardMarkup = None

    def load_dotenv(*_args, **_kwargs):
        return False

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "claims.db"
# New users must send this code with /join <code> to register.
# Change this value to your own private code before deployment.

NEW_CLAIM = "➕ নতুন Client"
SEARCH = "🔍 খুঁজুন"
HISTORY = "📜 History"
HELP = "❓ সাহায্য"
ADMIN_PANEL = "👑 Admin Panel"
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
                    joined_at TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    status TEXT NOT NULL DEFAULT 'active'
                )
            """)
            member_columns = {row["name"] for row in db.execute("PRAGMA table_info(members)")}
            if "role" not in member_columns:
                db.execute("ALTER TABLE members ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
            if "status" not in member_columns:
                db.execute("ALTER TABLE members ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")

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

    def add_member(self, user_id: int, name: str, role: str = "member") -> None:
        with self.session() as db:
            db.execute(
                """INSERT INTO members (user_id, user_name, joined_at, role, status)
                   VALUES (?, ?, ?, ?, 'active')
                   ON CONFLICT(user_id) DO UPDATE SET
                       user_name = excluded.user_name,
                       role = excluded.role""",
                (user_id, name, utc_now(), role),
            )

    def is_member(self, user_id: int) -> bool:
        with self.session() as db:
            return db.execute(
                "SELECT 1 FROM members WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ).fetchone() is not None

    def is_admin(self, user_id: int) -> bool:
        with self.session() as db:
            row = db.execute(
                "SELECT role, status FROM members WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return bool(row and row["role"] == "admin" and row["status"] == "active")

    def set_member_status(self, user_id: int, status: str) -> None:
        with self.session() as db:
            db.execute("UPDATE members SET status = ? WHERE user_id = ?", (status, user_id))

    def list_members(self) -> list[sqlite3.Row]:
        with self.session() as db:
            return db.execute(
                "SELECT user_id, user_name, joined_at, role, status FROM members ORDER BY joined_at DESC"
            ).fetchall()

    def active_member_count(self) -> int:
        with self.session() as db:
            return db.execute(
                "SELECT COUNT(*) FROM members WHERE status = 'active'"
            ).fetchone()[0]

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


def is_admin(context: ContextTypes.DEFAULT_TYPE, update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    settings: Settings = context.application.bot_data["settings"]
    store: ClaimStore = context.application.bot_data["store"]
    return user.id in settings.admins or store.is_admin(user.id)

def require_member(context: ContextTypes.DEFAULT_TYPE, update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    return is_admin(context, update) or context.application.bot_data["store"].is_member(user.id)

def main_menu(context=None, update=None):
    rows = [[HISTORY]]
    if context is not None and update is not None and is_admin(context, update):
        rows.append([ADMIN_PANEL])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        input_field_placeholder="History অথবা Admin Panel চাপুন",
    )

def claim_menu():
    return ReplyKeyboardMarkup(
        [[SAVE_CLAIM, CANCEL_CLAIM], [HISTORY]],
        resize_keyboard=True,
        input_field_placeholder="Client-এর তথ্য পাঠান অথবা Save Client চাপুন",
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
    store: ClaimStore = context.application.bot_data["store"]

    if code == ADMIN_JOIN_CODE:
        store.add_member(user.id, display_name(update), "admin")
        await update.effective_message.reply_text(
            "👑 Admin join successful!\n\nআপনি এখন Admin এবং সবার History দেখতে পারবেন।",
            reply_markup=main_menu(context, update),
        )
        return

    if code == TEAM_JOIN_CODE:
        store.add_member(user.id, display_name(update), "member")
        await update.effective_message.reply_text(
            "✅ Team join successful!",
            reply_markup=main_menu(context, update),
        )
        return

    await update.effective_message.reply_text(
        "❌ Code ভুল। 🔐 Join Team চাপুন এবং সঠিক Team Code দিন।"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: ClaimStore = context.application.bot_data["store"]
    user = update.effective_user
    if not user:
        return

    if is_admin(context, update):
        store.add_member(user.id, display_name(update), "admin")
        message = "👑 Admin Access Active!\n\nAdmin Panel এবং সবার History access চালু আছে।"
        markup = main_menu(context, update)
    elif store.is_member(user.id):
        message = "✅ আপনি team-এর member।"
        markup = main_menu(context, update)
    else:
        message = (
            "👋 Welcome!\n\n"
            "আপনি এখনও team-এ registered নন।\n"
            "🔐 শুধু Team Code পাঠান। সঠিক code হলে automatically login হয়ে যাবেন।"
        )
        markup = None

    await update.effective_message.reply_text(message, reply_markup=markup)

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(context, update):
        await reject(update, "❌ এই section শুধু Admin-এর জন্য।")
        return

    store: ClaimStore = context.application.bot_data["store"]
    active = store.active_member_count()
    total = len(store.list_members())
    rows = store.recent_all(50)

    await update.effective_message.reply_text(
        f"👑 ADMIN PANEL\n\n👥 Active Users: {active}\n👤 Total Registered: {total}\n📜 History Records: {len(rows)}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📜 History — সবার", callback_data="admin_history")],
            [InlineKeyboardButton("👥 Active Users / Ban-Control", callback_data="admin_users")],
        ]),
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not require_member(context, update):
        await reject(update, "আপনি registered নন। আগে সঠিক Team Code পাঠান।")
        return
    extra = "\nAdmins: /release <claim number>" if update.effective_user.id in settings.admins else ""
    await update.effective_message.reply_text("➕ নতুন Client চাপুন, তারপর নাম/link লিখুন বা screenshot পাঠান।\n🔍 খুঁজুন চাপুন, তারপর client-এর নাম, Facebook ID বা link লিখুন। Search পুরো database-এ duplicate check করবে।\n📜 History চাপুন: প্রত্যেক member শুধু নিজের claim history দেখবে; admin সব claim দেখতে পারবে." + extra, reply_markup=main_menu(context, update))


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
        await reject(update, "আপনি registered নন। আগে সঠিক Team Code পাঠান।")
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
    text = (update.effective_message.text or "").strip()

    # Easy join: users can simply send the code directly.
    # No Join Team button and no /join command are required.
    store: ClaimStore = context.application.bot_data["store"]
    user = update.effective_user

    if user and text == ADMIN_JOIN_CODE:
        store.add_member(user.id, display_name(update), "admin")
        context.user_data.pop("waiting_for", None)
        await update.effective_message.reply_text(
            "👑 Admin login successful!\n\n"
            "আপনি এখন Admin এবং সবার History দেখতে পারবেন।",
            reply_markup=main_menu(context, update),
        )
        return

    if user and text == TEAM_JOIN_CODE and not store.is_member(user.id):
        store.add_member(user.id, display_name(update), "member")
        context.user_data.pop("waiting_for", None)
        await update.effective_message.reply_text(
            "✅ Login successful!\n\n"
            "আপনি এখন Team-এর member।",
            reply_markup=main_menu(context, update),
        )
        return

    settings: Settings = context.application.bot_data["settings"]
    if not require_member(context, update):
        await reject(
            update,
            "🔒 আগে Join Team চাপুন এবং Admin-এর দেওয়া join code দিন।"
        )
        return

    if text == NEW_CLAIM:
        context.user_data["waiting_for"] = "collect_claim"
        context.user_data["draft"] = {"name": "", "link": "", "screenshot": ""}
        await update.effective_message.reply_text(
            "এখন নাম, Facebook link বা screenshot পাঠান।\n"
            "তথ্য পেলেই client automatically save হবে এবং duplicate check হবে।",
            reply_markup=claim_menu(),
        )
        return

    if text == CANCEL_CLAIM:
        context.user_data.pop("waiting_for", None)
        context.user_data.pop("draft", None)
        await update.effective_message.reply_text(
            "নতুন client যোগ করা বাতিল হয়েছে।",
            reply_markup=main_menu(context, update),
        )
        return

    if text == SAVE_CLAIM:
        draft = context.user_data.get("draft")
        if not draft:
            await update.effective_message.reply_text(
                "আগে ➕ নতুন Client চাপুন।",
                reply_markup=main_menu(context, update),
            )
            return
        original = "\n".join(
            part for part in (
                f"Name: {draft['name']}" if draft["name"] else "",
                draft["link"],
            ) if part
        )
        await save_claim(update, context, draft["screenshot"], original)
        return

    if text == SEARCH:
        context.user_data["waiting_for"] = "search"
        await update.effective_message.reply_text(
            "এখন client-এর নাম বা Facebook link লিখে Send করুন。",
            reply_markup=main_menu(context, update),
        )
        return

    if text == HISTORY:
        await history_command(update, context)
        return

    if text == HELP:
        await help_command(update, context)
        return

    if text == ADMIN_PANEL:
        await admin_panel(update, context)
        return

    waiting_for = context.user_data.get("waiting_for")
    if waiting_for == "collect_claim":
        draft = context.user_data["draft"]
        name, link = extract_client_fields(text)
        if name:
            draft["name"] = name
        if link:
            draft["link"] = link

        original = "\n".join(
            part for part in (
                f"Name: {draft['name']}" if draft["name"] else "",
                draft["link"],
            ) if part
        )
        if original or draft.get("screenshot"):
            await save_claim(
                update, context, draft.get("screenshot", ""), original or None
            )
            return

        await show_draft_status(update, context)
        return

    if waiting_for == "search":
        rows = context.application.bot_data["store"].search(text.lower())
        await update.effective_message.reply_text(
            "কোনো matching client পাওয়া যায়নি।"
            if not rows
            else "🔍 Search result / খোঁজার ফল\n\n"
                 + "\n\n".join(search_result_text(row) for row in rows),
            reply_markup=main_menu(context, update),
        )
        context.user_data.pop("waiting_for", None)
        return

    # Any ordinary text from a registered user is treated as client information.
    # No "New Client" button is required.
    try:
        await save_claim(update, context, "", text)
        return
    except ValueError:
        await update.effective_message.reply_text(
            "Client-এর নাম অথবা Facebook link/ID পাঠান।"
        )
        return


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not require_member(context, update):
        await reject(update, "আপনি registered নন। আগে সঠিক Team Code পাঠান।")
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
        await reject(update, "আপনি registered নন। আগে সঠিক Team Code পাঠান।")
        return

    store: ClaimStore = context.application.bot_data["store"]
    admin_access = is_admin(context, update)

    # Admins may review the team history; regular members can only see their own.
    rows = store.recent_all(50) if admin_access else store.recent_for_user(update.effective_user.id, 20)

    if not rows:
        title = "📜 All History / সব History" if admin_access else "📜 My History / আমার claim history"
        await update.effective_message.reply_text(
            f"{title}\n\nকোনো claim পাওয়া যায়নি।",
            reply_markup=main_menu(context, update),
        )
        return

    title = "📜 All History / সব History" if admin_access else "📜 My History / আমার claim history"
    await update.effective_message.reply_text(
        title + "\n\n" + "\n\n".join(row_text(row) for row in rows),
        reply_markup=main_menu(context, update),
    )


async def release_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not is_admin(context, update):
        await reject(update, "Only configured admins can release claims.")
        return
    try:
        claim_id = int(command_arg(update))
    except ValueError:
        await update.effective_message.reply_text("Usage: /release <claim number> (for example, /release 12)")
        return
    changed = context.application.bot_data["store"].release(claim_id, update.effective_user.id, display_name(update))
    await update.effective_message.reply_text("Claim released; its history was kept." if changed else "Active claim not found.")



async def admin_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(context, update):
        await update.callback_query.answer("Admin only.", show_alert=True)
        return
    query = update.callback_query
    await query.answer()
    rows = context.application.bot_data["store"].recent_all(50)
    if not rows:
        await query.edit_message_text("📜 এখনো কোনো claim history নেই।")
        return
    await query.edit_message_text(
        "📜 Latest 50 Claims / সর্বশেষ ৫০টি History\n\n"
        + "\n\n".join(row_text(row) for row in rows)
    )


async def admin_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(context, update):
        await update.callback_query.answer("Admin only.", show_alert=True)
        return
    query = update.callback_query
    await query.answer()
    store = context.application.bot_data["store"]
    members = store.list_members()
    lines = [
        f"👥 Active Users: {store.active_member_count()}",
        f"👤 Total Registered: {len(members)}",
        "",
    ]
    keyboard = []

    for member in members[:50]:
        uid = member["user_id"]
        name = member["user_name"] or str(uid)
        role = member["role"]
        status = member["status"]
        icon = "👑" if role == "admin" else ("🚫" if status == "banned" else "👤")
        lines.append(f"{icon} {name} — {uid} — {status}")

        if uid != update.effective_user.id and role != "admin":
            action = "unban" if status == "banned" else "ban"
            keyboard.append([
                InlineKeyboardButton(
                    f"{'✅ Unban' if action == 'unban' else '🚫 Ban'} {name[:20]}",
                    callback_data=f"admin_{action}_{uid}",
                )
            ])

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


async def admin_user_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(context, update):
        await update.callback_query.answer("Admin only.", show_alert=True)
        return

    query = update.callback_query
    await query.answer()
    parts = query.data.split("_", 2)
    if len(parts) != 3:
        return

    action, raw_uid = parts[1], parts[2]
    try:
        uid = int(raw_uid)
    except ValueError:
        return

    store = context.application.bot_data["store"]

    if uid == update.effective_user.id:
        await query.answer("নিজেকে ban করা যাবে না।", show_alert=True)
        return
    if store.is_admin(uid):
        await query.answer("Admin account ban করা যাবে না।", show_alert=True)
        return

    if action == "ban":
        store.set_member_status(uid, "banned")
        await query.edit_message_text(f"🚫 User {uid} banned.")
    elif action == "unban":
        store.set_member_status(uid, "active")
        await query.edit_message_text(f"✅ User {uid} unbanned.")


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or BOT_TOKEN
    members = parse_id_list(os.getenv("TEAM_MEMBER_IDS", ""))
    admins = parse_id_list(ADMIN_USER_IDS)
    chat_id = os.getenv("ALLOWED_CHAT_ID", "").strip()
    duplicate_message = os.getenv(
        "DUPLICATE_MESSAGE",
        "🚫 Duplicate Client Alert / ডুপ্লিকেট ক্লায়েন্ট সতর্কতা\n\nএই client-টি আগে থেকেই {claimed_by}-এর নেওয়া আছে।\nThis client is already claimed by {claimed_by}.\n\n✨ দয়া করে নতুন client দিন / Please choose another client.",
    ).strip()
    if not token or token == "put_your_botfather_token_here":
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in your local .env file.")
    # TEAM_MEMBER_IDS is kept for backward compatibility, but new members are stored in claims.db.
    return Settings(token, members, admins, int(chat_id) if chat_id else None, duplicate_message)


def main() -> None:
    if not DEPENDENCIES_AVAILABLE:
        raise RuntimeError("Install the packages first: py -m pip install -r requirements.txt")

    settings = load_settings()
    store = ClaimStore(DB_PATH)
    app = Application.builder().token(settings.token).build()

    app.bot_data.update(settings=settings, store=store)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(MessageHandler(filters.PHOTO, photo_claim))
    app.add_handler(CommandHandler("claim", claim_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("release", release_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_input))

    logging.info("Bot is starting. Press Ctrl+C to stop.")
    app.add_handler(CallbackQueryHandler(admin_history_callback, pattern=r"^admin_history$"))
    app.add_handler(CallbackQueryHandler(admin_users_callback, pattern=r"^admin_users$"))
    app.add_handler(CallbackQueryHandler(admin_user_action, pattern=r"^admin_(ban|unban)_\d+$"))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    main()
