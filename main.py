import json
import logging
import os
import asyncio
from datetime import datetime, timezone, timedelta
from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from aiohttp import web, ClientSession

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# CẤU HÌNH TOKEN VÀ KHÓA GROQ API
TELEGRAM_BOT_TOKEN = "9217105329:AAFt7sRUrrMXfKMhL-nNbUVqDtQSsR5CvgQ"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")

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


async def ask_ai(prompt: str) -> str:
    """Tự động truy vấn danh sách model khả dụng trên tài khoản Groq và trả lời"""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    async with ClientSession() as session:
        active_model = "llama-3.1-8b-instant"
        try:
            async with session.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=10) as m_resp:
                if m_resp.status == 200:
                    m_data = await m_resp.json()
                    model_list = [m["id"] for m in m_data.get("data", []) if "whisper" not in m["id"]]
                    for preferred in ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "llama3-70b-8192", "llama3-8b-8192", "gemma2-9b-it", "mixtral-8x7b-32768"]:
                        if preferred in model_list:
                            active_model = preferred
                            break
                    else:
                        if model_list:
                            active_model = model_list[0]
        except Exception as e:
            logger.warning(f"Không thể lấy danh sách model động: {e}")

        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = {
            "model": active_model,
            "messages": [
                {
                    "role": "system",
                    "content": "Bạn là trợ lý học tập và ôn tập kiến thức chuyên nghiệp. Hãy tóm tắt và đặt câu hỏi ôn tập Active Recall bằng tiếng Việt súc tích, gợi mở tư duy thực tế."
                },
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.6
        }

        try:
            async with session.post(url, json=payload, headers=headers, timeout=25) as resp:
                data = await resp.json()
                if resp.status == 200:
                    choices = data.get("choices", [])
                    if choices:
                        return choices[0].get("message", {}).get("content", "").strip()
                    return "Không nhận được phản hồi từ AI."
                else:
                    err_msg = data.get("error", {}).get("message", str(data))
                    logger.error(f"Lỗi Groq ({resp.status}): {err_msg}")
                    return f"Lỗi Groq ({resp.status}) với model [{active_model}]: {err_msg}"
        except Exception as e:
            logger.error(f"Lỗi kết nối Groq: {e}")
            return f"Lỗi kết nối mạng: {str(e)}"


HELP_MESSAGE = (
    "📖 **BẢNG TRA CỨU CÂU LỆNH MẪU**\n\n"
    "📝 **1. LƯU BÀI HỌC VÀ CHU KỲ NHẮC:**\n"
    "• `/save <nội dung>`: Lưu bài (nhắc chuẩn lặp lại)\n"
    "• `/save trongngay <nội dung>`: Nhắc ngay tối nay\n"
    "• `/save homsau <nội dung>`: Nhắc sau 1 ngày (ngày mai)\n"
    "• `/save 2ngay <nội dung>`: Nhắc sau 2 ngày (ngày kế tiếp)\n"
    "• `/save 3ngay <nội dung>`: Nhắc sau 3 ngày\n"
    "• `/save 7ngay <nội dung>`: Nhắc sau 7 ngày (1 tuần)\n"
    "• `/save hangngay <nội dung>`: Nhắc bài hàng ngày\n"
    "*(Ví dụ: `/save 3ngay Quy trình kiểm kê định kỳ bưu cục`)*\n\n"
    "🔍 **2. XEM LẠI & TÌM KIẾM BÀI CŨ:**\n"
    "• `/list`: Xem danh sách tất cả các bài đã lưu kèm mã #ID\n"
    "• `/search <từ khóa>`: Tìm kiếm bài theo nội dung\n"
    "*(Ví dụ: `/search bưu cục`)*\n\n"
    "🎯 **3. ÔN TẬP VỚI CÂU HỎI AI:**\n"
    "• `/ontap`: Ôn tập bài gần nhất vừa lưu\n"
    "• `/ontap <id>`: Ôn tập đích danh một bài cũ bất kỳ\n"
    "*(Ví dụ: `/ontap 2`)*\n\n"
    "⚙️ **4. CÀI ĐẶT THỜI GIAN NHẮC:**\n"
    "• `/settime <giờ_sáng> <giờ_tối> <giờ_nhắc>`\n"
    "*(Ví dụ: `/settime 09:00 19:30 22:00`)*\n\n"
    "💡 *Mẹo: Bạn có thể nhắn tin hỏi đáp tự do bất kỳ lúc nào!*"
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hiển thị bảng danh sách các câu lệnh mẫu"""
    await update.message.reply_text(HELP_MESSAGE, parse_mode="Markdown")


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

    welcome_text = (
        f"Chào {user_name}! 👋\n\n"
        "Tôi là **Trợ lý Ôn tập & Nhắc nhở Kiến thức Thông minh** (Groq AI).\n\n"
        "Gõ lệnh **/help** để mở bảng tra cứu toàn bộ câu lệnh mẫu bất cứ lúc nào."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")
    await update.message.reply_text(HELP_MESSAGE, parse_mode="Markdown")


async def settime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = context.args

    if not args or len(args) != 3:
        await update.message.reply_text(
            "⚠️ Vui lòng nhập đủ 3 khung giờ theo định dạng HH:MM.\nVí dụ:\n`/settime 09:00 20:00 22:30`",
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
        f"✅ **Đã cập nhật lịch nhắc mới!**\n\n"
        f"☀️ Sáng: **{m_time}**\n"
        f"🎯 Tối: **{e_time}**\n"
        f"🌙 Nhắc bài: **{n_time}**",
        parse_mode="Markdown",
    )


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = context.args
    now = datetime.now(LOCAL_TZ)

    if not args:
        await update.message.reply_text(
            "⚠️ Vui lòng nhập nội dung sau lệnh `/save`.\n"
            "Cú pháp mẫu: `/save [chu_kỳ] <nội dung>`\n"
            "Ví dụ:\n`/save 3ngay Quy trình kiểm tra định kỳ bưu cục`\n\n"
            "💡 Gõ `/help` để xem đầy đủ các tùy chọn chu kỳ.",
            parse_mode="Markdown",
        )
        return

    mode_map = {
        "hangngay": "hangngay",
        "trongngay": "trongngay",
        "homsau": "1ngay",
        "ngayhomsau": "1ngay",
        "1ngay": "1ngay",
        "ketiep": "2ngay",
        "ngayketiep": "2ngay",
        "2ngay": "2ngay",
        "3ngay": "3ngay",
        "7ngay": "7ngay"
    }

    first_arg = args[0].lower().replace("-", "").replace("_", "")
    if first_arg in mode_map:
        cycle = mode_map[first_arg]
        content = " ".join(args[1:]).strip()
    else:
        cycle = "hangngay"
        content = " ".join(args).strip()

    if not content:
        await update.message.reply_text("⚠️ Bạn chưa điền nội dung cần lưu!")
        return

    today_date = now.date()
    target_date = today_date
    cycle_label = "Hằng ngày"

    if cycle == "trongngay":
        target_date = today_date
        cycle_label = "Trong ngày hôm nay"
    elif cycle == "1ngay":
        target_date = today_date + timedelta(days=1)
        cycle_label = "Ngày hôm sau (sau 1 ngày)"
    elif cycle == "2ngay":
        target_date = today_date + timedelta(days=2)
        cycle_label = "Ngày kế tiếp (sau 2 ngày)"
    elif cycle == "3ngay":
        target_date = today_date + timedelta(days=3)
        cycle_label = "Sau 3 ngày"
    elif cycle == "7ngay":
        target_date = today_date + timedelta(days=7)
        cycle_label = "Sau 7 ngày (1 tuần)"

    data = load_data()
    if chat_id not in data["users"]:
        data["users"][chat_id] = {
            "name": update.effective_user.first_name,
            "registered_at": now.isoformat(),
            "notes": [],
            "schedule": {"morning": "10:00", "evening": "19:30", "night": "22:00"},
            "last_sent": {},
        }

    note_id = len(data["users"][chat_id]["notes"]) + 1
    new_note = {
        "id": note_id,
        "date": now.strftime("%Y-%m-%d %H:%M"),
        "created_date": today_date.isoformat(),
        "target_date": target_date.isoformat(),
        "cycle": cycle,
        "cycle_label": cycle_label,
        "content": content,
    }
    data["users"][chat_id]["notes"].append(new_note)
    save_data(data)

    confirm_text = (
        f"✅ **Đã ghi nhận kiến thức [Mã #{note_id}]!**\n\n"
        f"📝 *Nội dung:* {content}\n"
        f"⏰ *Chu kỳ nhắc:* **{cycle_label}**\n"
        f"📅 *Lịch bắt đầu nhắc:* `{target_date.strftime('%d/%m/%Y')}`\n\n"
        f"💡 Dùng `/ontap {note_id}` để ôn lại bài này bất cứ lúc nào."
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

    notes = user_info["notes"]
    target_note = None

    if context.args:
        try:
            target_id = int(context.args[0])
            for n in notes:
                if n.get("id") == target_id:
                    target_note = n
                    break
            if not target_note:
                await update.message.reply_text(f"❌ Không tìm thấy bài học có mã `#{target_id}`. Dùng `/list` để xem danh sách.")
                return
        except ValueError:
            await update.message.reply_text("⚠️ Mã bài học phải là số. Ví dụ: `/ontap 1`")
            return
    else:
        target_note = notes[-1]

    await update.message.reply_text("🤖 Groq AI đang soạn câu hỏi gợi nhớ...")

    prompt = (
        f"Dưới đây là kiến thức đã lưu: '{target_note['content']}'.\n"
        "Đóng vai trợ lý học tập Active Recall: Hãy tóm tắt 1 câu cốt lõi ngắn, sau đó đặt 2 câu hỏi tình huống thực tế để kiểm tra và khắc sâu trí nhớ. Trả lời bằng tiếng Việt ngắn gọn, gợi mở."
    )
    ai_question = await ask_ai(prompt)

    msg = f"📚 **Ôn tập bài [#{target_note.get('id', 'Mới')}] lưu ngày {target_note['date']}:**\n\n{ai_question}"
    await update.message.reply_text(msg)


async def list_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    user_info = data.get("users", {}).get(chat_id)

    if not user_info or not user_info.get("notes"):
        await update.message.reply_text("Hiện tại bạn chưa lưu bài học nào. Hãy dùng `/save` để bắt đầu!")
        return

    notes = user_info["notes"]
    reply = f"📚 **Danh sách {len(notes)} bài học đã lưu của bạn:**\n\n"

    recent_notes = notes[-10:]
    for item in recent_notes:
        n_id = item.get("id", "-")
        c_label = item.get("cycle_label", "Hằng ngày")
        reply += f"🔹 **[#{n_id}]** `{item['date']}` *(Chu kỳ: {c_label})*\n"
        reply += f"👉 {item['content'][:120]}...\n"
        reply += f"*(Ôn bài này: `/ontap {n_id}`)*\n\n"

    if len(notes) > 10:
        reply += f"💡 *Đang hiển thị 10 bài gần nhất. Dùng `/search <từ khóa>` để tìm bài cũ hơn.*"

    await update.message.reply_text(reply, parse_mode="Markdown")


async def search_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    keyword = " ".join(context.args).strip().lower() if context.args else ""

    if not keyword:
        await update.message.reply_text(
            "⚠️ Vui lòng nhập từ khóa cần tìm kiếm.\nVí dụ:\n`/search bưu cục` hoặc `/search kpi`",
            parse_mode="Markdown",
        )
        return

    data = load_data()
    user_info = data.get("users", {}).get(chat_id)

    if not user_info or not user_info.get("notes"):
        await update.message.reply_text("Kho dữ liệu học tập của bạn hiện đang trống!")
        return

    matched = [n for n in user_info["notes"] if keyword in n["content"].lower()]

    if not matched:
        await update.message.reply_text(f"🔍 Không tìm thấy bài học nào chứa từ khóa: **{keyword}**", parse_mode="Markdown")
        return

    reply = f"🔍 **Tìm thấy {len(matched)} bài học chứa từ khóa '{keyword}':**\n\n"
    for item in matched[:5]:
        n_id = item.get("id", "-")
        reply += f"📌 **[#{n_id}]** `({item['date']})`:\n{item['content']}\n"
        reply += f"*(Gõ `/ontap {n_id}` để kiểm tra)*\n\n"

    await update.message.reply_text(reply, parse_mode="Markdown")


async def handle_chat_with_groq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    await update.message.chat.send_action("typing")

    prompt = (
        f"Người dùng nhắn: '{user_text}'.\n"
        "Trả lời bằng tiếng Việt thân thiện, súc tích. Nếu người dùng muốn lưu bài học, hãy hướng dẫn họ dùng lệnh /save <chu_kỳ> <nội dung> hoặc gõ /help để xem danh sách lệnh."
    )
    ai_reply = await ask_ai(prompt)
    await update.message.reply_text(ai_reply)


async def dynamic_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(LOCAL_TZ)
    current_time_str = now.strftime("%H:%M")
    today_date = now.date()
    today_str = today_date.isoformat()

    data = load_data()
    updated = False

    for chat_id, user_data in data.get("users", {}).items():
        schedule = user_data.get(
            "schedule", {"morning": "10:00", "evening": "19:30", "night": "22:00"}
        )
        last_sent = user_data.get("last_sent", {})
        notes = user_data.get("notes", [])

        if current_time_str == schedule.get("night", "22:00"):
            if last_sent.get("night") != today_str:
                msg = (
                    "🌙 **Đến giờ tổng kết kiến thức ngày hôm nay!**\n\n"
                    "Hôm nay bạn học được điều gì mới hoặc rút ra kinh nghiệm gì? Hãy dùng lệnh sau để lưu lại nhé:\n"
                    "`/save [chu_kỳ] <nội dung>`\n"
                    "*(Ví dụ: `/save 3ngay Quy trình giao nhận hàng lỗi`)*\n\n"
                    "💡 Gõ `/help` để xem bảng câu lệnh."
                )
                try:
                    await context.bot.send_message(chat_id=int(chat_id), text=msg, parse_mode="Markdown")
                    last_sent["night"] = today_str
                    updated = True
                except Exception as e:
                    logger.error(f"Lỗi gửi tin nhắc đêm: {e}")

        is_morning = (current_time_str == schedule.get("morning", "10:00")) and (last_sent.get("morning") != today_str)
        is_evening = (current_time_str == schedule.get("evening", "19:30")) and (last_sent.get("evening") != today_str)

        if is_morning or is_evening:
            due_notes = []
            for n in notes:
                cycle = n.get("cycle", "hangngay")
                t_date_str = n.get("target_date")
                
                if cycle == "hangngay":
                    due_notes.append(n)
                elif t_date_str and t_date_str == today_str:
                    due_notes.append(n)

            if due_notes:
                selected_note = due_notes[-1]
                session_type = "buổi sáng" if is_morning else "thực chiến buổi tối"
                prompt = (
                    f"Ghi chú đã học: '{selected_note['content']}'.\n"
                    f"Hãy đặt 1 câu hỏi đánh giá nhanh Active Recall dành cho {session_type} bằng tiếng Việt để giúp người học nhớ lâu."
                )
                ai_q = await ask_ai(prompt)
                
                title = "☀️ **Active Recall Sáng Nay:**" if is_morning else "🎯 **Tình Huống Vận Dụng Tối Nay:**"
                msg_body = (
                    f"{title}\n\n"
                    f"📖 *Bài học #{selected_note.get('id', '')}:* {selected_note['content'][:100]}...\n\n"
                    f"{ai_q}\n\n"
                    f"💡 *(Gõ `/ontap {selected_note.get('id', '')}` để xem giải nghĩa đầy đủ)*"
                )
                try:
                    await context.bot.send_message(chat_id=int(chat_id), text=msg_body, parse_mode="Markdown")
                    if is_morning:
                        last_sent["morning"] = today_str
                    if is_evening:
                        last_sent["evening"] = today_str
                    updated = True
                except Exception as e:
                    logger.error(f"Lỗi gửi tin nhắc định kỳ: {e}")

        user_data["last_sent"] = last_sent

    if updated:
        save_data(data)


async def handle_ping(request):
    return web.Response(text="Bot Telegram Tro Ly On Tap dang hoat dong 24/7!")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Đã mở cổng {port} trên Render thành công!")


async def setup_bot_commands(application):
    """Tự động thiết lập danh sách lệnh hiển thị trên thanh gõ Telegram"""
    commands = [
        BotCommand("help", "Bảng tra cứu toàn bộ câu lệnh mẫu"),
        BotCommand("save", "Lưu bài học theo chu kỳ (homsau, 2ngay, 3ngay...)"),
        BotCommand("ontap", "Ôn tập kiến thức với AI (hoặc /ontap <id>)"),
        BotCommand("list", "Xem danh sách bài học đã lưu"),
        BotCommand("search", "Tìm kiếm bài học cũ theo từ khóa"),
        BotCommand("settime", "Cài đặt 3 khung giờ nhắc (sáng, tối, đêm)"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Đã cập nhật danh sách lệnh trên thanh menu Telegram thành công!")
    except Exception as e:
        logger.error(f"Lỗi cấu hình bot commands: {e}")


async def run_bot():
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(setup_bot_commands)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("lenh", help_command))
    application.add_handler(CommandHandler("save", save_command))
    application.add_handler(CommandHandler("ontap", ontap_command))
    application.add_handler(CommandHandler("list", list_notes_command))
    application.add_handler(CommandHandler("search", search_notes_command))
    application.add_handler(CommandHandler("settime", settime_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_with_groq))

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(dynamic_scheduler_job, interval=60, first=10)

    await run_web_server()

    async with application:
        await application.start()
        await application.updater.start_polling()
        logger.info("🚀 Bot đang hoạt động với đầy đủ tính năng mới...")
        while True:
            await asyncio.sleep(3600)


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
