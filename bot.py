#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║           CAPTCHA EARNING BOT — bot.py                       ║
║           Single-file | Python 3.11+ | PTB v20+              ║
║           Database: SQLite | CAPTCHA: Pillow                 ║
╚══════════════════════════════════════════════════════════════╝
"""

# ─────────────────────────────────────────────────────────────
#  IMPORTS
# ─────────────────────────────────────────────────────────────
import os
import io
import re
import random
import string
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION  (edit these before deploying)
# ─────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
BOT_USERNAME: str = os.environ.get("BOT_USERNAME", "YourBotUsername")

OWNER_ID: int = 8499435987          # Telegram user ID of the owner/admin
SUPPORT_LINK: str = "[t.me](https://t.me/YourSupportUsername)"

ACTIVATION_PAYMENT_NUMBER: str = "01705930972"
ACTIVATION_PAYMENT_METHODS: str = "bKash / Nagad"

DB_PATH: str = "captcha_bot.db"

# Earn / penalty
REWARD_CORRECT: int = 2             # TK earned per correct CAPTCHA
PENALTY_WRONG: int = 1              # TK deducted per wrong answer

# Referral
REFERRAL_BONUS: int = 20            # TK given to referrer

# Withdrawal
MIN_WITHDRAWAL: int = 100           # Minimum TK to withdraw

# Cooldown between CAPTCHA attempts (seconds)
CAPTCHA_COOLDOWN: int = 15

# CAPTCHA auto-expiry (seconds)
CAPTCHA_EXPIRY: int = 60

# Timezone for timestamps
TZ = ZoneInfo("Asia/Dhaka")

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  CONVERSATION STATES
# ─────────────────────────────────────────────────────────────
(
    STATE_CAPTCHA_ANSWER,
    STATE_WITHDRAW_AMOUNT,
    STATE_ACTIVATION_CONFIRM,
) = range(3)

# ─────────────────────────────────────────────────────────────
#  DATABASE SETUP
# ─────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with row factory."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create all tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id          INTEGER PRIMARY KEY,
                full_name        TEXT    NOT NULL,
                username         TEXT,
                balance          INTEGER NOT NULL DEFAULT 0,
                is_active        INTEGER NOT NULL DEFAULT 0,
                is_banned        INTEGER NOT NULL DEFAULT 0,
                joined_at        TEXT    NOT NULL,
                referred_by      INTEGER,
                referral_count   INTEGER NOT NULL DEFAULT 0,
                total_earned     INTEGER NOT NULL DEFAULT 0,
                last_captcha_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS captchas (
                user_id     INTEGER PRIMARY KEY,
                answer      TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                expires_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS withdrawals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                amount      INTEGER NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending',
                created_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS activation_requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending',
                created_at  TEXT    NOT NULL
            );
        """)
    logger.info("Database initialised at %s", DB_PATH)


# ─────────────────────────────────────────────────────────────
#  DATABASE HELPERS
# ─────────────────────────────────────────────────────────────

def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %I:%M %p")


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def upsert_user(user_id: int, full_name: str, username: str | None) -> None:
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, full_name, username, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = excluded.full_name,
                username  = excluded.username
        """, (user_id, full_name, username, now_str()))


def get_user(user_id: int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def update_balance(user_id: int, delta: int) -> int:
    """Add delta to balance (clamped to >= 0). Returns new balance."""
    with get_db() as conn:
        conn.execute("""
            UPDATE users
            SET balance = MAX(0, balance + ?)
            WHERE user_id = ?
        """, (delta, user_id))
    return get_user(user_id)["balance"]


def set_balance(user_id: int, amount: int) -> int:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET balance = MAX(0, ?) WHERE user_id = ?",
            (amount, user_id)
        )
    return get_user(user_id)["balance"]


def add_earned(user_id: int, amount: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET total_earned = total_earned + ? WHERE user_id = ?",
            (amount, user_id)
        )


def set_last_captcha(user_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET last_captcha_at = ? WHERE user_id = ?",
            (now_iso(), user_id)
        )


def store_captcha(user_id: int, answer: str) -> None:
    expires = (datetime.now(TZ) + timedelta(seconds=CAPTCHA_EXPIRY)).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO captchas (user_id, answer, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                answer     = excluded.answer,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
        """, (user_id, answer, now_iso(), expires))


def get_captcha(user_id: int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM captchas WHERE user_id = ?", (user_id,)
        ).fetchone()


def delete_captcha(user_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM captchas WHERE user_id = ?", (user_id,))


def is_captcha_expired(captcha_row: sqlite3.Row) -> bool:
    expires = datetime.fromisoformat(captcha_row["expires_at"])
    return datetime.now(TZ) > expires


def increment_referral_count(referrer_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET referral_count = referral_count + 1 WHERE user_id = ?",
            (referrer_id,)
        )


def set_referred_by(user_id: int, referrer_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET referred_by = ? WHERE user_id = ? AND referred_by IS NULL",
            (referrer_id, user_id)
        )


def has_pending_withdrawal(user_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM withdrawals WHERE user_id = ? AND status = 'pending'",
            (user_id,)
        ).fetchone()
    return row is not None


def create_withdrawal(user_id: int, amount: int) -> int:
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO withdrawals (user_id, amount, status, created_at)
            VALUES (?, ?, 'pending', ?)
        """, (user_id, amount, now_iso()))
    return cur.lastrowid


def has_pending_activation(user_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM activation_requests WHERE user_id = ? AND status = 'pending'",
            (user_id,)
        ).fetchone()
    return row is not None


def create_activation_request(user_id: int) -> int:
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO activation_requests (user_id, status, created_at)
            VALUES (?, 'pending', ?)
        """, (user_id, now_iso()))
    return cur.lastrowid


def set_user_active(user_id: int, active: bool) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET is_active = ? WHERE user_id = ?",
            (1 if active else 0, user_id)
        )
        if active:
            conn.execute(
                "UPDATE activation_requests SET status = 'approved' WHERE user_id = ? AND status = 'pending'",
                (user_id,)
            )


def set_user_banned(user_id: int, banned: bool) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET is_banned = ? WHERE user_id = ?",
            (1 if banned else 0, user_id)
        )


def get_stats() -> dict:
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_active=1").fetchone()["c"]
        inactive = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_active=0").fetchone()["c"]
        banned = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_banned=1").fetchone()["c"]
        withdrawals = conn.execute("SELECT COUNT(*) as c FROM withdrawals").fetchone()["c"]
        total_rewards = conn.execute("SELECT COALESCE(SUM(total_earned),0) as s FROM users").fetchone()["s"]
    return {
        "total": total,
        "active": active,
        "inactive": inactive,
        "banned": banned,
        "withdrawals": withdrawals,
        "total_rewards": total_rewards,
    }


# ─────────────────────────────────────────────────────────────
#  CAPTCHA IMAGE GENERATOR
# ─────────────────────────────────────────────────────────────

def generate_captcha_text(length: int = 5) -> str:
    """Generate a random alphanumeric CAPTCHA string."""
    pool = string.ascii_uppercase + string.digits
    # Remove easily confused characters
    pool = pool.replace("0", "").replace("O", "").replace("1", "").replace("I", "")
    return "".join(random.choices(pool, k=length))


def generate_captcha_image(text: str) -> io.BytesIO:
    """
    Render the CAPTCHA text as a distorted image using Pillow.
    Returns a BytesIO PNG buffer.
    """
    width, height = 220, 80
    bg_color = (240, 248, 230)
    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)

    # Noise dots
    for _ in range(60):
        x = random.randint(0, width)
        y = random.randint(0, height)
        draw.point((x, y), fill=(random.randint(50, 120), random.randint(80, 160), random.randint(50, 120)))

    # Noise lines
    for _ in range(4):
        x1, y1 = random.randint(0, width), random.randint(0, height)
        x2, y2 = random.randint(0, width), random.randint(0, height)
        draw.line([(x1, y1), (x2, y2)], fill=(100, 140, 100), width=1)

    # Try to load a TTF font; fall back to default
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
    except OSError:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 42)
        except OSError:
            font = ImageFont.load_default()

    # Draw each character with slight random offset and color variation
    x_cursor = 12
    for ch in text:
        color = (
            random.randint(20, 80),
            random.randint(100, 160),
            random.randint(20, 80),
        )
        y_offset = random.randint(-6, 6)
        draw.text((x_cursor, 15 + y_offset), ch, font=font, fill=color)
        x_cursor += random.randint(36, 42)

    # Slight blur for realism
    img = img.filter(ImageFilter.GaussianBlur(radius=0.8))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────────────────────

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🎯 Earn", "👤 Account"],
        ["💼 Wallet", "👥 Referral"],
        ["📤 Withdraw", "🛟 Support"],
        ["⚙️ Menu"],
    ],
    resize_keyboard=True,
    input_field_placeholder="Choose an option…",
)

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔐 Activate Your Account"],
        ["🔙 Back"],
    ],
    resize_keyboard=True,
)

ACTIVATION_INLINE = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("✅ Done", callback_data="activation_done"),
        InlineKeyboardButton("❌ Cancel", callback_data="activation_cancel"),
    ]
])


# ─────────────────────────────────────────────────────────────
#  UTILITY HELPERS
# ─────────────────────────────────────────────────────────────

def fmt_name(user) -> str:
    """Return escaped full name."""
    return escape_markdown(user.full_name, version=2)


def fmt_username(username: str | None) -> str:
    if username:
        return f"@{escape_markdown(username, version=2)}"
    return "N/A"


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


async def send_owner_alert(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Send a plain-text alert to the bot owner."""
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:
        logger.warning("Could not alert owner: %s", exc)


def guard_banned(user_row: sqlite3.Row) -> bool:
    """Return True if the user is banned."""
    return bool(user_row["is_banned"])


# ─────────────────────────────────────────────────────────────
#  /start HANDLER
# ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Register user, handle referral, send welcome, alert owner."""
    tg_user = update.effective_user
    user_id = tg_user.id
    full_name = tg_user.full_name
    username = tg_user.username

    existing = get_user(user_id)
    upsert_user(user_id, full_name, username)
    user_row = get_user(user_id)

    if user_row and guard_banned(user_row):
        await update.message.reply_text(
            "🚫 Your account has been suspended\\. Please contact support\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ConversationHandler.END

    # ── Referral processing ──────────────────────────────────
    referral_msg = ""
    if context.args and not existing:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg[4:])
                if referrer_id != user_id:
                    set_referred_by(user_id, referrer_id)
                    referrer = get_user(referrer_id)
                    if referrer:
                        new_bal = update_balance(referrer_id, REFERRAL_BONUS)
                        add_earned(referrer_id, REFERRAL_BONUS)
                        increment_referral_count(referrer_id)
                        referral_msg = (
                            f"🎉 You joined via a referral link\\!"
                        )
                        # Notify referrer
                        try:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=(
                                    f"🎉 *New Referral Joined\\!*\n"
                                    f"━━━━━━━━━━━━━━━\n"
                                    f"👤 *{escape_markdown(full_name, version=2)}* joined using your link\\.\n"
                                    f"💰 *\\+{REFERRAL_BONUS} TK* added to your wallet\\.\n"
                                    f"🏦 New Balance: *{new_bal} TK*"
                                ),
                                parse_mode=ParseMode.MARKDOWN_V2,
                            )
                        except Exception:
                            pass
            except (ValueError, TypeError):
                pass

    # ── Owner alert for new users ────────────────────────────
    if not existing:
        uname_display = f"@{username}" if username else "None"
        alert = (
            f"👤 *New User Started Bot\\!*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"• Name: {escape_markdown(full_name, version=2)}\n"
            f"• Username: {escape_markdown(uname_display, version=2)}\n"
            f"• User ID: `{user_id}`"
        )
        await send_owner_alert(context, alert)

    # ── Welcome message ──────────────────────────────────────
    welcome = (
        f"👋 *Welcome to Captcha Bot\\!*\n\n"
        f"🎯 Solve CAPTCHA challenges and earn *TK rewards* instantly\\.\n"
        f"💡 Use the menu below to get started\\.\n"
    )
    if referral_msg:
        welcome += f"\n{referral_msg}\n"

    await update.message.reply_text(
        welcome,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────
#  EARN — CAPTCHA FLOW
# ─────────────────────────────────────────────────────────────

async def handle_earn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send a CAPTCHA image and await the user's answer."""
    user = update.effective_user
    user_row = get_user(user.id)

    if not user_row:
        await update.message.reply_text("Please send /start first.")
        return ConversationHandler.END

    if guard_banned(user_row):
        await update.message.reply_text("🚫 Your account is suspended.")
        return ConversationHandler.END

    # ── Cooldown check ───────────────────────────────────────
    if user_row["last_captcha_at"]:
        last = datetime.fromisoformat(user_row["last_captcha_at"])
        elapsed = (datetime.now(TZ) - last).total_seconds()
        remaining = CAPTCHA_COOLDOWN - elapsed
        if remaining > 0:
            await update.message.reply_text(
                f"⏳ Please wait *{int(remaining)+1}s* before solving another CAPTCHA\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return ConversationHandler.END

    # ── Generate CAPTCHA ─────────────────────────────────────
    captcha_text = generate_captcha_text()
    captcha_img = generate_captcha_image(captcha_text)
    store_captcha(user.id, captcha_text)
    set_last_captcha(user.id)

    await update.message.reply_photo(
        photo=InputFile(captcha_img, filename="captcha.png"),
        caption=(
            "🎯 *Solve the CAPTCHA to earn \\+2 TK\\!*\n\n"
            "📝 Type the text shown in the image exactly\\.\n"
            f"⏱ You have *{CAPTCHA_EXPIRY} seconds* to answer\\.\n"
            "⚠️ Wrong answer deducts 1 TK\\."
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_CAPTCHA_ANSWER


async def handle_captcha_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate the CAPTCHA answer and reward or penalise."""
    user = update.effective_user
    user_input = update.message.text.strip().upper()

    captcha_row = get_captcha(user.id)
    if not captcha_row:
        await update.message.reply_text(
            "⚠️ No active CAPTCHA found\\. Tap *🎯 Earn* to get a new one\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    if is_captcha_expired(captcha_row):
        delete_captcha(user.id)
        await update.message.reply_text(
            "⏰ *CAPTCHA Expired\\!*\nYour time ran out\\. Tap *🎯 Earn* to try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    correct_answer = captcha_row["answer"].upper()
    delete_captcha(user.id)

    if user_input == correct_answer:
        new_bal = update_balance(user.id, REWARD_CORRECT)
        add_earned(user.id, REWARD_CORRECT)
        await update.message.reply_text(
            f"✅ *Correct Answer\\!* Well done\\!\n\n"
            f"💰 *\\+{REWARD_CORRECT} TK* earned\\.\n"
            f"🏦 New Balance: *{new_bal} TK*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        new_bal = update_balance(user.id, -PENALTY_WRONG)
        await update.message.reply_text(
            f"❌ *Wrong Answer\\!*\n"
            f"The correct text was: `{escape_markdown(correct_answer, version=2)}`\n\n"
            f"💸 *\\-{PENALTY_WRONG} TK* deducted\\.\n"
            f"🏦 New Balance: *{new_bal} TK*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )

    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────
#  ACCOUNT
# ─────────────────────────────────────────────────────────────

async def handle_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row = get_user(user.id)
    if not row:
        await update.message.reply_text("Please send /start first.")
        return

    if guard_banned(row):
        await update.message.reply_text("🚫 Your account is suspended.")
        return

    uname = f"@{row['username']}" if row["username"] else "N/A"
    status = "🟢 Active" if row["is_active"] else "🔴 Inactive"

    text = (
        f"👤 *Account Information*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔹 *Name:* {escape_markdown(row['full_name'], version=2)}\n"
        f"🔹 *Username:* {escape_markdown(uname, version=2)}\n"
        f"🔹 *User ID:* `{row['user_id']}`\n"
        f"🔹 *Status:* {escape_markdown(status, version=2)}\n"
        f"🔹 *Joined:* {escape_markdown(row['joined_at'], version=2)}\n"
        f"🔹 *Referrals:* {row['referral_count']}\n"
        f"🔹 *Total Earned:* {row['total_earned']} TK"
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=MAIN_KEYBOARD
    )


# ─────────────────────────────────────────────────────────────
#  WALLET
# ─────────────────────────────────────────────────────────────

async def handle_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row = get_user(user.id)
    if not row:
        await update.message.reply_text("Please send /start first.")
        return

    if guard_banned(row):
        await update.message.reply_text("🚫 Your account is suspended.")
        return

    await update.message.reply_text(
        f"💼 *Wallet Information*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 *Owner:* {escape_markdown(row['full_name'], version=2)}\n"
        f"💰 *Balance:* `{row['balance']} TK`\n"
        f"📈 *Total Earned:* `{row['total_earned']} TK`",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=MAIN_KEYBOARD,
    )


# ─────────────────────────────────────────────────────────────
#  REFERRAL
# ─────────────────────────────────────────────────────────────

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row = get_user(user.id)
    if not row:
        await update.message.reply_text("Please send /start first.")
        return

    if guard_banned(row):
        await update.message.reply_text("🚫 Your account is suspended.")
        return

    ref_link = f"[t.me](https://t.me/{BOT_USERNAME}?start=ref_{user.id})"
    await update.message.reply_text(
        f"👥 *Referral Program*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎁 Invite friends and earn *{REFERRAL_BONUS} TK* for every valid join\\!\n\n"
        f"🔗 *Your Referral Link:*\n"
        f"`{escape_markdown(ref_link, version=2)}`\n\n"
        f"👤 *Total Referrals:* {row['referral_count']}\n\n"
        f"📌 _Rules:_\n"
        f"• Reward is given once per unique user\n"
        f"• You cannot refer yourself\n"
        f"• New user must start the bot via your link",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=MAIN_KEYBOARD,
    )


# ─────────────────────────────────────────────────────────────
#  WITHDRAW FLOW
# ─────────────────────────────────────────────────────────────

async def handle_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    row = get_user(user.id)
    if not row:
        await update.message.reply_text("Please send /start first.")
        return ConversationHandler.END

    if guard_banned(row):
        await update.message.reply_text("🚫 Your account is suspended.")
        return ConversationHandler.END

    if has_pending_withdrawal(user.id):
        await update.message.reply_text(
            "⏳ *Withdrawal Pending*\n\n"
            "You already have a pending withdrawal request\\.\n"
            "Please wait for admin to process it\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"📤 *Withdrawal Request*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 *Current Balance:* `{row['balance']} TK`\n"
        f"📌 *Minimum Withdrawal:* `{MIN_WITHDRAWAL} TK`\n\n"
        f"💬 Please enter the amount you wish to withdraw:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_WITHDRAW_AMOUNT


async def handle_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    row = get_user(user.id)
    text = update.message.text.strip()

    # ── Validate amount ──────────────────────────────────────
    if not text.isdigit():
        await update.message.reply_text(
            "⚠️ Please enter a valid *whole number* amount\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return STATE_WITHDRAW_AMOUNT

    amount = int(text)

    if amount < MIN_WITHDRAWAL:
        await update.message.reply_text(
            f"❌ *Minimum withdrawal is {MIN_WITHDRAWAL} TK*\\.\n"
            f"You entered: `{amount} TK`\\.\n\n"
            f"Please enter a higher amount or tap /start to return\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    if amount > row["balance"]:
        await update.message.reply_text(
            f"❌ *Insufficient Balance*\n\n"
            f"You requested `{amount} TK` but only have `{row['balance']} TK`\\.\n"
            f"Keep earning and try again\\!",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    # ── Inactive check ───────────────────────────────────────
    if not row["is_active"]:
        await update.message.reply_text(
            "⚠️ *Account Activation Required*\n"
            "━━━━━━━━━━━━━━━\n"
            "Your account is not yet eligible for withdrawal\\.\n\n"
            "📌 To unlock withdrawals, please activate your account:\n"
            "👉 Go to *⚙️ Menu → 🔐 Activate Your Account*\n\n"
            "_Activation is quick and easy\\. Complete it to start withdrawing\\!_",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    # ── Process withdrawal ───────────────────────────────────
    create_withdrawal(user.id, amount)
    new_balance = update_balance(user.id, -amount)

    uname_display = f"@{row['username']}" if row["username"] else "None"
    timestamp = now_str()

    # Admin alert
    alert = (
        f"📤 *New Withdrawal Request\\!*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 *Name:* {escape_markdown(row['full_name'], version=2)}\n"
        f"🔗 *Username:* {escape_markdown(uname_display, version=2)}\n"
        f"🆔 *User ID:* `{user.id}`\n"
        f"💸 *Amount:* `{amount} TK`\n"
        f"💰 *Balance After:* `{new_balance} TK`\n"
        f"📌 *Status:* Active\n"
        f"⏰ *Time:* {escape_markdown(timestamp, version=2)}"
    )
    await send_owner_alert(context, alert)

    await update.message.reply_text(
        f"✅ *Withdrawal Request Submitted\\!*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💸 *Amount:* `{amount} TK`\n"
        f"💰 *Remaining Balance:* `{new_balance} TK`\n\n"
        f"⏳ Your request has been sent to admin\\. "
        f"Processing usually takes 24–48 hours\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────
#  SUPPORT
# ─────────────────────────────────────────────────────────────

async def handle_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row = get_user(user.id)
    if row and guard_banned(row):
        await update.message.reply_text("🚫 Your account is suspended.")
        return

    await update.message.reply_text(
        "🛟 *Support Center*\n"
        "━━━━━━━━━━━━━━━\n"
        "Having trouble? Our support team is here to help\\.\n\n"
        "📬 Tap the button below to contact support:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Contact Support", url=SUPPORT_LINK)]
        ]),
    )


# ─────────────────────────────────────────────────────────────
#  MENU / ACTIVATION FLOW
# ─────────────────────────────────────────────────────────────

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row = get_user(user.id)
    if row and guard_banned(row):
        await update.message.reply_text("🚫 Your account is suspended.")
        return

    await update.message.reply_text(
        "⚙️ *Main Menu*\n"
        "━━━━━━━━━━━━━━━\n"
        "Select an option below:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=MENU_KEYBOARD,
    )


async def handle_activate_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show payment instructions and Done/Cancel buttons."""
    user = update.effective_user
    row = get_user(user.id)

    if not row:
        await update.message.reply_text("Please send /start first.")
        return ConversationHandler.END

    if guard_banned(row):
        await update.message.reply_text("🚫 Your account is suspended.")
        return ConversationHandler.END

    if row["is_active"]:
        await update.message.reply_text(
            "✅ *Your account is already active\\!*\n"
            "You can withdraw your earnings anytime\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    if has_pending_activation(user.id):
        await update.message.reply_text(
            "⏳ *Activation Pending*\n\n"
            "You already have a pending activation request\\.\n"
            "Please wait for admin to review and approve it\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"🔐 *Activate Your Account*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"To unlock withdrawals, please send the activation fee to:\n\n"
        f"📱 *Number:* `{ACTIVATION_PAYMENT_NUMBER}`\n"
        f"💳 *Methods:* {escape_markdown(ACTIVATION_PAYMENT_METHODS, version=2)}\n\n"
        f"After sending, tap *✅ Done* below\\.\n"
        f"Admin will verify and activate your account shortly\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=ACTIVATION_INLINE,
    )
    return STATE_ACTIVATION_CONFIRM


async def handle_activation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Done / Cancel inline button presses for activation."""
    query = update.callback_query
    await query.answer()
    user = query.from_user
    row = get_user(user.id)

    if query.data == "activation_done":
        create_activation_request(user.id)

        uname_display = f"@{row['username']}" if row["username"] else "None"
        timestamp = now_str()

        alert = (
            f"🔐 *New Activation Claim\\!*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 *Name:* {escape_markdown(row['full_name'], version=2)}\n"
            f"🔗 *Username:* {escape_markdown(uname_display, version=2)}\n"
            f"🆔 *User ID:* `{user.id}`\n"
            f"⏰ *Time:* {escape_markdown(timestamp, version=2)}\n\n"
            f"_Use /active {user.id} to approve\\._"
        )
        await send_owner_alert(context, alert)

        await query.edit_message_text(
            "✅ *Activation Request Submitted\\!*\n\n"
            "Your request has been sent to admin\\.\n"
            "You will be notified once your account is activated\\.\n\n"
            "⏳ _Typical processing time: a few hours\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await context.bot.send_message(
            chat_id=user.id,
            text="🏠 Returning to main menu\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )

    elif query.data == "activation_cancel":
        await query.edit_message_text(
            "❌ *Activation Cancelled*\n\n"
            "You can activate your account anytime from *⚙️ Menu*\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await context.bot.send_message(
            chat_id=user.id,
            text="🏠 Returning to main menu\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )

    return ConversationHandler.END


async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🏠 *Main Menu*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=MAIN_KEYBOARD,
    )


# ─────────────────────────────────────────────────────────────
#  ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────

def admin_only(func):
    """Decorator: reject non-owner users from admin commands."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("🚫 Unauthorised.")
            return
        await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


@admin_only
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /check <user_id>")
        return
    uid = int(context.args[0])
    row = get_user(uid)
    if not row:
        await update.message.reply_text(f"❌ User `{uid}` not found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    uname = f"@{row['username']}" if row["username"] else "N/A"
    status = "🟢 Active" if row["is_active"] else "🔴 Inactive"
    banned = "⛔ Banned" if row["is_banned"] else "✅ Not Banned"
    await update.message.reply_text(
        f"👤 *User Details*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔹 Name: {escape_markdown(row['full_name'], version=2)}\n"
        f"🔹 Username: {escape_markdown(uname, version=2)}\n"
        f"🔹 User ID: `{uid}`\n"
        f"🔹 Balance: `{row['balance']} TK`\n"
        f"🔹 Total Earned: `{row['total_earned']} TK`\n"
        f"🔹 Status: {escape_markdown(status, version=2)}\n"
        f"🔹 Banned: {escape_markdown(banned, version=2)}\n"
        f"🔹 Referrals: {row['referral_count']}\n"
        f"🔹 Joined: {escape_markdown(row['joined_at'], version=2)}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@admin_only
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /add <user_id> <amount>")
        return
    uid, amount = int(context.args[0]), int(context.args[1])
    if not get_user(uid):
        await update.message.reply_text(f"❌ User `{uid}` not found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    new_bal = update_balance(uid, amount)
    await update.message.reply_text(
        f"✅ Added `{amount} TK` to user `{uid}`\\.\nNew balance: `{new_bal} TK`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    logger.info("ADMIN: added %d TK to user %d at %s", amount, uid, now_str())


@admin_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /remove <user_id> <amount>")
        return
    uid, amount = int(context.args[0]), int(context.args[1])
    if not get_user(uid):
        await update.message.reply_text(f"❌ User `{uid}` not found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    new_bal = update_balance(uid, -amount)
    await update.message.reply_text(
        f"✅ Removed `{amount} TK` from user `{uid}`\\.\nNew balance: `{new_bal} TK`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    logger.info("ADMIN: removed %d TK from user %d at %s", amount, uid, now_str())


@admin_only
async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /set <user_id> <amount>")
        return
    uid, amount = int(context.args[0]), int(context.args[1])
    if not get_user(uid):
        await update.message.reply_text(f"❌ User `{uid}` not found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    new_bal = set_balance(uid, amount)
    await update.message.reply_text(
        f"✅ Balance of user `{uid}` set to `{new_bal} TK`\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    uid = int(context.args[0])
    if not get_user(uid):
        await update.message.reply_text(f"❌ User `{uid}` not found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    set_user_banned(uid, True)
    await update.message.reply_text(
        f"⛔ User `{uid}` has been *banned*\\.", parse_mode=ParseMode.MARKDOWN_V2
    )
    logger.info("ADMIN: banned user %d at %s", uid, now_str())


@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    uid = int(context.args[0])
    if not get_user(uid):
        await update.message.reply_text(f"❌ User `{uid}` not found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    set_user_banned(uid, False)
    await update.message.reply_text(
        f"✅ User `{uid}` has been *unbanned*\\.", parse_mode=ParseMode.MARKDOWN_V2
    )


@admin_only
async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /active <user_id>")
        return
    uid = int(context.args[0])
    row = get_user(uid)
    if not row:
        await update.message.reply_text(f"❌ User `{uid}` not found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    set_user_active(uid, True)
    await update.message.reply_text(
        f"✅ User `{uid}` has been *activated*\\.", parse_mode=ParseMode.MARKDOWN_V2
    )
    # Notify the user
    try:
        await context.bot.send_message(
            chat_id=uid,
            text=(
                "🎉 *Account Activated\\!*\n\n"
                "Your account has been verified and activated by admin\\.\n"
                "You can now submit withdrawal requests\\! 💸"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=MAIN_KEYBOARD,
        )
    except Exception:
        pass
    logger.info("ADMIN: activated user %d at %s", uid, now_str())


@admin_only
async def cmd_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /inactive <user_id>")
        return
    uid = int(context.args[0])
    if not get_user(uid):
        await update.message.reply_text(f"❌ User `{uid}` not found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    set_user_active(uid, False)
    await update.message.reply_text(
        f"🔴 User `{uid}` has been set to *inactive*\\.", parse_mode=ParseMode.MARKDOWN_V2
    )


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_stats()
    await update.message.reply_text(
        f"📊 *Bot Statistics*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👥 Total Users: `{s['total']}`\n"
        f"🟢 Active: `{s['active']}`\n"
        f"🔴 Inactive: `{s['inactive']}`\n"
        f"⛔ Banned: `{s['banned']}`\n"
        f"📤 Total Withdrawals: `{s['withdrawals']}`\n"
        f"💰 Total Rewards Paid: `{s['total_rewards']} TK`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg = " ".join(context.args)
    with get_db() as conn:
        user_ids = [row[0] for row in conn.execute("SELECT user_id FROM users").fetchall()]

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            sent += 1
            await asyncio.sleep(0.05)   # rate-limit safety
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 Broadcast complete\\.\n✅ Sent: `{sent}` \\| ❌ Failed: `{failed}`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ─────────────────────────────────────────────────────────────
#  FALLBACK / UNKNOWN MESSAGE HANDLER
# ─────────────────────────────────────────────────────────────

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤔 I didn't understand that\\. Use the menu buttons or send /start\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=MAIN_KEYBOARD,
    )


# ─────────────────────────────────────────────────────────────
#  CONVERSATION HANDLER BUILDER
# ─────────────────────────────────────────────────────────────

def build_conversation_handler() -> ConversationHandler:
    """
    Single ConversationHandler managing:
      - Earn → CAPTCHA answer
      - Withdraw → amount input
      - Menu → Activate → Done/Cancel (via callback)
    """
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^🎯 Earn$"), handle_earn),
            MessageHandler(filters.Regex(r"^📤 Withdraw$"), handle_withdraw),
            MessageHandler(filters.Regex(r"^🔐 Activate Your Account$"), handle_activate_account),
        ],
        states={
            STATE_CAPTCHA_ANSWER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_captcha_answer),
            ],
            STATE_WITHDRAW_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_withdraw_amount),
            ],
            STATE_ACTIVATION_CONFIRM: [
                CallbackQueryHandler(handle_activation_callback, pattern=r"^activation_"),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown),
        ],
        allow_reentry=True,
        per_message=False,
    )


# ─────────────────────────────────────────────────────────────
#  APPLICATION SETUP & MAIN
# ─────────────────────────────────────────────────────────────

def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError(
            "BOT_TOKEN is not set. Export it as an environment variable:\n"
            "  export BOT_TOKEN='your_token_here'"
        )

    init_db()
    logger.info("Bot is running...")

    app = Application.builder().token(BOT_TOKEN).build()

    # ── Admin commands ───────────────────────────────────────
    app.add_handler(CommandHandler("check",     cmd_check))
    app.add_handler(CommandHandler("add",       cmd_add))
    app.add_handler(CommandHandler("remove",    cmd_remove))
    app.add_handler(CommandHandler("set",       cmd_set))
    app.add_handler(CommandHandler("ban",       cmd_ban))
    app.add_handler(CommandHandler("unban",     cmd_unban))
    app.add_handler(CommandHandler("active",    cmd_active))
    app.add_handler(CommandHandler("inactive",  cmd_inactive))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # ── /start ───────────────────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))

    # ── Conversation (Earn / Withdraw / Activate) ────────────
    app.add_handler(build_conversation_handler())

    # ── Simple menu buttons ──────────────────────────────────
    app.add_handler(MessageHandler(filters.Regex(r"^👤 Account$"),  handle_account))
    app.add_handler(MessageHandler(filters.Regex(r"^💼 Wallet$"),   handle_wallet))
    app.add_handler(MessageHandler(filters.Regex(r"^👥 Referral$"), handle_referral))
    app.add_handler(MessageHandler(filters.Regex(r"^🛟 Support$"),  handle_support))
    app.add_handler(MessageHandler(filters.Regex(r"^⚙️ Menu$"),    handle_menu))
    app.add_handler(MessageHandler(filters.Regex(r"^🔙 Back$"),     handle_back))

    # ── Fallback ─────────────────────────────────────────────
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
