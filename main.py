import json
import logging
import os
import asyncio
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from aiohttp import web
from google import genai

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = "9217105329:AAFt7sRUrrMXfKMhL-nNbUVqDtQSsR5CvgQ"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6IZTCF3QcO9oNVYnAhs82r9iNj1g8SglnT2NpbMAlA0cg")

LOCAL_TZ = timezone(timedelta(hours=7))
DATA_FILE = "study_notes.json"

client = genai.Client(api_key=GEMINI_API_KEY)


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


def ask_gemini(prompt: str) -> str:
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        logger.error(f"Lỗi gọi Gemini API: {e}")
        return "Xin lỗi, trợ lý AI hiện đang bận hoặc khóa API chưa cấu hình chính xác."


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_name = update.effective_user.first_name

    data = load_data()
    if chat_id not in data["users"]:
        data["users"][chat_id] = {
            "name": user_name,
            "registered_at": datetime.now(LOCAL_TZ).isoformat(),
            "notes": [],
            "schedule": {"morning": "10:00", "evening": "19:30", "night": "22:00"},
            "last_sent": {},
        }
        save_data(data)

    user_schedule = data["users"][chat_id].get(
        "schedule", {"morning": "10:00", "evening": "19:30", "night": "22:00"}
    )

    welcome_text = (
        f"Chào {user_name}! 👋\n\n"
        "Tôi là **Trợ lý Ôn tập Kiến thức Cá nhân** tích hợp Gemini AI.\n\n"
        "📌 **Các lệnh điều khiển:**\n"
        "• `/save <nội dung>`: Lưu kiến thức mới học.\n"
        "• `/ontap`: Ôn tập nhanh bài gần nhất với câu hỏi Active Recall từ Gemini.\n"
        "• `/list`: Xem danh sách 5 bài gần nhất.\n"
        "• `/settime <giờ_sáng> <giờ_tối> <giờ_nhắc>`: Tùy chỉnh giờ nhắc nhở theo ý muốn.\n"
        f"  *(Lịch hiện tại của bạn: Sáng {user_schedule.get('morning', '10:00')} | Tối {user_schedule.get('evening', '19:30')} | Nhắc {user_schedule.get('night', '22:00')})*\n\n"
        "💡 Bạn có thể trò chuyện tự do với tôi bất cứ lúc nào mà không sợ bị lưu nhầm bài học!"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def settime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = context.args

    if not args or len(args) != 3:
        await update.message.reply_text(
            "⚠️ Vui lòng nhập đủ 3 khung giờ theo định dạng HH:MM.\n\n"
            "Ví dụ:\n`/settime 09:00 20:00 22:30`\n"
            "(Trong đó: 09:00 là ôn sáng, 20:00 là ôn tối, 22:30 là nhắc gửi bài)",
            parse_mode="Markdown",
        )
        return

    m_time, e_time, n_time = args[0], args[1], args[2]

    for t_str in [m_time, e_time, n_time]:
        try:
            parts = t_str.split(":")
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
        except Exception:
            await update.message.reply_text(
                f"❌ Giờ `{t_str}` không hợp lệ. Vui lòng nhập định dạng 24h từ 00:00 đến 23:59.",
                parse_mode="Markdown",
            )
            return

    data = load_data()
    if chat_id not in data["users"]:
        data["users"][chat_id] = {
            "name": update.effective_user.first_name,
            "notes": [],
            "last_sent": {},
        }

    data["users"][chat_id]["schedule"] = {
        "morning": m_time,
        "evening": e_time,
        "night": n_time,
    }
    save_data(data)

    await update.message.reply_text(
        f"✅ **Đã cập nhật lịch nhắc mới thành công!**\n\n"
        f"☀️ Ôn tập Active Recall sáng: **{m_time}**\n"
        f"🎯 Ôn tập tình huống thực tế tối: **{e_time}**\n"
        f"🌙 Nhắc gửi kiến thức mới: **{n_time}**",
        parse_mode="Markdown",
    )


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    content = " ".join(context.args).strip() if context.args else ""
    now = datetime.now(LOCAL_TZ)

    if not content:
        await update.message.reply_text(
            "⚠️ Vui lòng nhập nội dung sau lệnh `/save`.\n"
            "Ví dụ:\n`/save Quy trình xuất nhập tồn kho bước 1 kiểm tra đơn...`",
            parse_mode="Markdown",
        )
        return

    data = load_data()
    if chat_id not in data["users"]:
        data["users"][chat_id] = {
            "name": update.effective_user.first_name,
            "registered_at": now.isoformat(),
            "notes": [],
            "schedule": {"morning": "10:00", "evening": "19:30", "night": "22:00"},
            "last_sent": {},
        }

    new_note = {
        "date": now.strftime("%Y-%m-%d %H:%M"),
        "timestamp": now.isoformat(),
        "content": content,
    }
    data["users"][chat_id]["notes"].append(new_note)
    save_data(data)

    confirm_text = (
        "✅ **Đã ghi nhận kiến thức thành công!**\n\n"
        f"📝 *Nội dung:*\n{content}\n\n"
        "🎯 Trợ lý sẽ tự động nhắc bạn theo lịch hẹn đã cài đặt. Bạn cũng có thể dùng `/ontap` để ôn lại bất cứ lúc nào."
    )
    await update.message.reply_text(confirm_text, parse_mode="Markdown")


async def ontap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    user_info = data.get("users", {}).get(chat_id)

    if not user_info or not user_info.get("notes"):
        await update.message.reply_text(
            "Bạn chưa lưu bài học nào! Hãy dùng lệnh `/save <nội dung>` để lưu bài đầu tiên."
        )
        return

    latest_note = user_info["notes"][-1]
    await update.message.reply_text("🤖 Gemini đang soạn câu hỏi gợi nhớ cho bạn...")

    prompt = (
        f"Dưới đây là ghi chú học tập của người dùng: '{latest_note['content']}'.\n"
        "Hãy đóng vai trợ lý ôn tập thông minh theo phương pháp gợi nhớ chủ động (Active Recall). "
        "Tóm tắt 1 câu ngắn gọn, sau đó đặt 2 câu hỏi tình huống hoặc kiểm tra trí nhớ để họ tự suy ngẫm. "
        "Lời văn thân thiện, súc tích."
    )
    ai_question = ask_gemini(prompt)

    msg = f"📚 **Ôn tập bài lưu ngày {latest_note['date']}:**\n\n{ai_question}"
    await update.message.reply_text(msg)


async def list_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    user_info = data.get("users", {}).get(chat_id)

    if not user_info or not user_info.get("notes"):
        await update.message.reply_text("Hiện tại bạn chưa lưu bài học nào. Hãy dùng `/save` để bắt đầu!")
        return

    notes = user_info["notes"][-5:]
    reply = "📚 **5 nội dung học tập gần nhất của bạn:**\n\n"
    for idx, item in enumerate(notes, 1):
        reply += f"{idx}. *[{item['date']}]*\n{item['content'][:150]}...\n\n"

    await update.message.reply_text(reply, parse_mode="Markdown")


async def handle_chat_with_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    await update.message.chat.send_action("typing")

    prompt = (
        f"Người dùng vừa nhắn: '{user_text}'.\n"
        "Bạn là trợ lý học tập cá nhân thân thiện. Hãy trả lời câu hỏi của người dùng ngắn gọn, hữu ích. "
        "Nếu người dùng có ý định lưu bài học, hãy nhắc họ dùng cú pháp /save <nội dung>."
    )
    ai_reply = ask_gemini(prompt)
    await update.message.reply_text(ai_reply)


async def dynamic_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(LOCAL_TZ)
    current_time_str = now.strftime("%H:%M")
    today_date_str = now.strftime("%Y-%m-%d")

    data = load_data()
    updated = False

    for chat_id, user_data in data.get("users", {}).items():
        schedule = user_data.get(
            "schedule", {"morning": "10:00", "evening": "19:30", "night": "22:00"}
        )
        last_sent = user_data.get("last_sent", {})

        # 1. Nhắc nhở buổi tối
        if current_time_str == schedule.get("night", "22:00"):
            if last_sent.get("night") != today_date_str:
                msg = (
                    "🌙 **Đã đến giờ tổng kết ngày rồi!**\n\n"
                    "Hôm nay bạn học được điều gì hay hoặc xử lý tình huống nào tâm đắc nhất?\n"
                    "👉 Dùng cú pháp: `/save <nội dung>` để tôi lưu lại và lên lịch ôn tập cho bạn nhé!"
                )
                try:
                    await context.bot.send_message(chat_id=int(chat_id), text=msg, parse_mode="Markdown")
                    last_sent["night"] = today_date_str
                    updated = True
                except Exception as e:
                    logger.error(f"Lỗi gửi tin nhắc tối: {e}")

        # 2. Ôn tập buổi sáng
        if current_time_str == schedule.get("morning", "10:00"):
            if last_sent.get("morning") != today_date_str:
                notes = user_data.get("notes", [])
                if notes:
                    content = notes[-1]["content"]
                    prompt = (
                        f"Kiến thức người dùng đã ghi lại: '{content}'.\n"
                        "Hãy tạo 1 câu hỏi nhanh Active Recall để đánh thức trí nhớ buổi sáng."
                    )
                    ai_q = ask_gemini(prompt)
                    msg = f"☀️ **Khởi động trí nhớ buổi sáng (Active Recall)!**\n\n{ai_q}"
                    try:
                        await context.bot.send_message(chat_id=int(chat_id), text=msg)
                        last_sent["morning"] = today_date_str
                        updated = True
                    except Exception as e:
                        logger.error(f"Lỗi gửi tin sáng: {e}")

        # 3. Ôn tập tình huống buổi tối
        if current_time_str == schedule.get("evening", "19:30"):
            if last_sent.get("evening") != today_date_str:
                notes = user_data.get("notes", [])
                if notes:
                    content = notes[-1]["content"]
                    prompt = (
                        f"Kiến thức người dùng đã ghi lại: '{content}'.\n"
                        "Hãy đặt 1 câu hỏi tình huống thực tế chuyên sâu để người dùng vận dụng kiến thức này."
                    )
                    ai_q = ask_gemini(prompt)
                    msg = f"🎯 **Ôn tập vận dụng thực tế!**\n\n{ai_q}"
                    try:
                        await context.bot.send_message(chat_id=int(chat_id), text=msg)
                        last_sent["evening"] = today_date_str
                        updated = True
                    except Exception as e:
                        logger.error(f"Lỗi gửi tin tối: {e}")

        user_data["last_sent"] = last_sent

    if updated:
        save_data(data)


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
    application.add_handler(CommandHandler("save", save_command))
    application.add_handler(CommandHandler("ontap", ontap_command))
    application.add_handler(CommandHandler("list", list_notes_command))
    application.add_handler(CommandHandler("settime", settime_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_with_gemini))

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(dynamic_scheduler_job, interval=60, first=10)
        logger.info("Da kich hoat tien trinh kiem tra lich hen dong!")

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
