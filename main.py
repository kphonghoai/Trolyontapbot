import json
import logging
import os
import asyncio
from datetime import datetime, time, timezone, timedelta
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from aiohttp import web

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = "9217105329:AAFt7sRUrrMXfKMhL-nNbUVqDtQSsR5CvgQ"
LOCAL_TZ = timezone(timedelta(hours=7))
DATA_FILE = "study_notes.json"


def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Lỗi đọc file: {e}")
    return {"users": {}}


def save_data(data: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Lỗi ghi file: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_name = update.effective_user.first_name

    data = load_data()
    if chat_id not in data["users"]:
        data["users"][chat_id] = {
            "name": user_name,
            "registered_at": datetime.now(LOCAL_TZ).isoformat(),
            "notes": [],
        }
        save_data(data)

    welcome_text = (
        f"Chào {user_name}! 👋\n\n"
        "Tôi là Trợ lý Ôn tập Kiến thức của bạn.\n\n"
        "⏰ Lịch hoạt động mỗi ngày:\n"
        "• 22:00: Tôi sẽ nhắc bạn gửi kiến thức mới học hôm nay.\n"
        "• 10:00 sáng hôm sau: Gửi tóm tắt + câu hỏi gợi nhớ nhanh (Active Recall).\n"
        "• 19:30 tối hôm sau: Câu hỏi thực hành & tình huống chuyên sâu.\n\n"
        "💡 Bạn có thể gửi nội dung kiến thức vào đây bất cứ lúc nào!"
    )
    await update.message.reply_text(welcome_text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    content = update.message.text
    now = datetime.now(LOCAL_TZ)

    data = load_data()
    if chat_id not in data["users"]:
        data["users"][chat_id] = {
            "name": update.effective_user.first_name,
            "registered_at": now.isoformat(),
            "notes": [],
        }

    new_note = {
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": now.isoformat(),
        "content": content,
    }
    data["users"][chat_id]["notes"].append(new_note)
    save_data(data)

    confirm_text = (
        "✅ Đã ghi nhận kiến thức hôm nay!\n\n"
        f"📝 Nội dung:\n{content}\n\n"
        "🎯 Lịch ôn tập: Tôi sẽ nhắc bạn ôn tập nội dung này vào 10:00 sáng và 19:30 tối ngày mai."
    )
    await update.message.reply_text(confirm_text)


async def list_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    user_info = data["users"].get(chat_id)

    if not user_info or not user_info.get("notes"):
        await update.message.reply_text("Hiện tại bạn chưa lưu bài học nào. Hãy gửi nội dung để bắt đầu nhé!")
        return

    notes = user_info["notes"][-5:]
    reply = "📚 5 nội dung học tập gần nhất của bạn:\n\n"
    for idx, item in enumerate(notes, 1):
        reply += f"{idx}. Ngày {item['date']}:\n{item['content'][:150]}...\n\n"

    await update.message.reply_text(reply)


async def job_reminder_22h(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    message = (
        "🌙 Đã 22:00 rồi!\n\n"
        "Hôm nay bạn đã học được điều gì mới và tâm đắc nhất?\n"
        "👉 Hãy gửi nội dung kiến thức vào đây để tôi lên lịch nhắc bạn ôn tập vào ngày mai nhé!"
    )
    for chat_id in data.get("users", {}):
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=message)
        except Exception as e:
            logger.error(f"Lỗi gửi tin nhắn 22h: {e}")


async def job_review_10h(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    for chat_id, info in data.get("users", {}).items():
        notes = info.get("notes", [])
        if not notes:
            continue
        content = notes[-1]["content"]
        message = (
            "☀️ 10:00 Sáng - Thời điểm kích hoạt trí nhớ (Active Recall)!\n\n"
            f"📖 Nội dung bạn đã học tối qua:\n{content}\n\n"
            "🧠 Thử thách nhanh: Không nhìn lại nội dung trên, bạn hãy thử tóm tắt lại 3 ý chính "
            "hoặc tự giải thích lại khái niệm này trong 1 câu xem sao nhé!"
        )
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=message)
        except Exception as e:
            logger.error(f"Lỗi gửi tin nhắn 10h: {e}")


async def job_review_19h30(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    for chat_id, info in data.get("users", {}).items():
        notes = info.get("notes", [])
        if not notes:
            continue
        content = notes[-1]["content"]
        message = (
            "🎯 19:30 Tối - Ôn tập chuyên sâu & Vận dụng thực tế!\n\n"
            f"Về chủ đề hôm nay bạn đã học:\n{content[:200]}...\n\n"
            "💡 Câu hỏi tình huống:\n"
            "Nếu áp dụng kiến thức này vào công việc hoặc thực tế ngay ngày mai, "
            "bước đầu tiên bạn sẽ làm là gì? Hãy trả lời để khắc sâu kiến thức nhé!"
        )
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=message)
        except Exception as e:
            logger.error(f"Lỗi gửi tin nhắn 19h30: {e}")


# Web server giả lập để Render quét mở cổng thành công
async def handle_ping(request):
    return web.Response(text="Bot Telegram dang hoat dong 24/7!")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Da mo cong web {port} thanh cong cho Render!")


async def run_bot():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("list", list_notes_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_daily(job_reminder_22h, time=time(hour=22, minute=0, tzinfo=LOCAL_TZ))
        job_queue.run_daily(job_review_10h, time=time(hour=10, minute=0, tzinfo=LOCAL_TZ))
        job_queue.run_daily(job_review_19h30, time=time(hour=19, minute=30, tzinfo=LOCAL_TZ))
        logger.info("Da len lich nhac nho 10:00, 19:30 va 22:00!")

    await run_web_server()

    async with application:
        await application.start()
        await application.updater.start_polling()
        logger.info("🚀 Bot dang chay...")
        while True:
            await asyncio.sleep(3600)


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
