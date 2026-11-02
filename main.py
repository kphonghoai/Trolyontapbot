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

# CẤU HÌNH TOKEN & KHÓA API
TELEGRAM_BOT_TOKEN = "9217105329:AAFt7sRUrrMXfKMhL-nNbUVqDtQSsR5CvgQ"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")

# KHÓA CỦA LARK SUITE (Điền App ID và App Secret của bạn vào đây)
LARK_APP_ID = os.environ.get("LARK_APP_ID", "cli_DAN_APP_ID_CUA_BAN_VAO_DAY")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "DAN_APP_SECRET_CUA_BAN_VAO_DAY")

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
    """Tự động truy vấn model Groq và xử lý câu trả lời"""
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
                    for preferred in ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "llama3-70b-8192", "gemma2-9b-it"]:
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


# =====================================================================
# BỘ XỬ LÝ LÔ-GÍC CHUNG (DÙNG CHO CẢ TELEGRAM VÀ LARK SUITE)
# =====================================================================

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


def process_user_action(user_key: str, user_name: str, text: str) -> str:
    """Xử lý lệnh và trả về chuỗi phản hồi (dùng chung cho Telegram & Lark)"""
    text = text.strip()
    data = load_data()
    now = datetime.now(LOCAL_TZ)

    if user_key not in data["users"]:
        data["users"][user_key] = {
            "name": user_name,
            "registered_at": now.isoformat(),
            "notes": [],
            "schedule": {"morning": "10:00", "evening": "19:30", "night": "22:00"},
            "last_sent": {},
        }
        save_data(data)

    parts = text.split()
    cmd = parts[0].lower() if parts else ""
    args = parts[1:]

    if cmd in ["/start", "/help", "/lenh"]:
        return HELP_MESSAGE

    if cmd == "/save":
        if not args:
            return "⚠️ Vui lòng nhập nội dung sau lệnh `/save`.\nVí dụ: `/save 3ngay Quy trình giao nhận`"

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
            return "⚠️ Bạn chưa điền nội dung cần lưu!"

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

        note_id = len(data["users"][user_key]["notes"]) + 1
        new_note = {
            "id": note_id,
            "date": now.strftime("%Y-%m-%d %H:%M"),
            "created_date": today_date.isoformat(),
            "target_date": target_date.isoformat(),
            "cycle": cycle,
            "cycle_label": cycle_label,
            "content": content,
        }
        data["users"][user_key]["notes"].append(new_note)
        save_data(data)

        return (
            f"✅ Đã ghi nhận kiến thức [Mã #{note_id}]!\n\n"
            f"📝 Nội dung: {content}\n"
            f"⏰ Chu kỳ nhắc: {cycle_label}\n"
            f"📅 Lịch bắt đầu nhắc: {target_date.strftime('%d/%m/%Y')}\n\n"
            f"💡 Dùng `/ontap {note_id}` để ôn lại bài này."
        )

    if cmd == "/list":
        notes = data["users"][user_key].get("notes", [])
        if not notes:
            return "Hiện tại bạn chưa lưu bài học nào. Hãy dùng `/save` để bắt đầu!"

        reply = f"📚 Danh sách {len(notes)} bài học đã lưu của bạn:\n\n"
        for item in notes[-10:]:
            n_id = item.get("id", "-")
            c_label = item.get("cycle_label", "Hằng ngày")
            reply += f"🔹 [#{n_id}] {item['date']} (Chu kỳ: {c_label})\n👉 {item['content'][:120]}...\n(Ôn bài: `/ontap {n_id}`)\n\n"
        return reply

    if cmd == "/search":
        keyword = " ".join(args).strip().lower()
        if not keyword:
            return "⚠️ Vui lòng nhập từ khóa. Ví dụ: `/search bưu cục`"
        notes = data["users"][user_key].get("notes", [])
        matched = [n for n in notes if keyword in n["content"].lower()]
        if not matched:
            return f"🔍 Không tìm thấy bài học nào chứa từ khóa: {keyword}"
        reply = f"🔍 Tìm thấy {len(matched)} bài học:\n\n"
        for item in matched[:5]:
            reply += f"📌 [#{item.get('id', '-')}] ({item['date']}):\n{item['content']}\n\n"
        return reply

    if cmd == "/settime":
        if len(args) != 3:
            return "⚠️ Vui lòng nhập đủ 3 khung giờ HH:MM. Ví dụ: `/settime 09:00 19:30 22:00`"
        data["users"][user_key]["schedule"] = {"morning": args[0], "evening": args[1], "night": args[2]}
        save_data(data)
        return f"✅ Đã cập nhật lịch nhắc mới: Sáng {args[0]} | Tối {args[1]} | Đêm {args[2]}"

    return ""


# =====================================================================
# MODULE KẾT NỐI LARK SUITE (LARK OPEN API & WEBHOOK)
# =====================================================================

async def get_lark_tenant_token(session: ClientSession) -> str:
    """Lấy access token từ Lark Suite bằng App ID & App Secret"""
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            data = await resp.json()
            return data.get("tenant_access_token", "")
    except Exception as e:
        logger.error(f"Lỗi lấy Lark tenant_access_token: {e}")
        return ""


async def send_lark_message(chat_id: str, text: str):
    """Gửi tin nhắn phản hồi về khung chat trên Lark Suite"""
    async with ClientSession() as session:
        token = await get_lark_tenant_token(session)
        if not token:
            return

        url = "https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text})
        }
        try:
            async with session.post(url, json=payload, headers=headers, timeout=15) as resp:
                data = await resp.json()
                if resp.status != 200 or data.get("code") != 0:
                    logger.error(f"Lỗi gửi tin Lark: {data}")
        except Exception as e:
            logger.error(f"Lỗi kết nối API Lark: {e}")


async def handle_lark_webhook(request):
    """Tiếp nhận và xử lý sự kiện tin nhắn từ Lark Suite"""
    try:
        body = await request.json()
    except Exception:
        return web.Response(text="Bad Request", status=400)

    # 1. Trả về mã challenge khi cấu hình Event URL lần đầu trên Lark Console
    if "challenge" in body:
        return web.json_response({"challenge": body["challenge"]})

    # 2. Xử lý tin nhắn người dùng gửi đến
    event = body.get("event", {})
    msg_info = event.get("message", {})
    sender = event.get("sender", {}).get("sender_id", {})
    chat_id = msg_info.get("chat_id")

    if chat_id and msg_info.get("message_type") == "text":
        try:
            content_json = json.loads(msg_info.get("content", "{}"))
            user_text = content_json.get("text", "").strip()
        except Exception:
            user_text = ""

        if user_text:
            lark_user_key = f"lark_{sender.get('open_id', chat_id)}"
            
            # Xử lý lệnh nhanh (/save, /list, /search, /help...)
            direct_reply = process_user_action(lark_user_key, "Lark User", user_text)
            if direct_reply:
                await send_lark_message(chat_id, direct_reply)
            elif user_text.startswith("/ontap"):
                # Ôn tập bằng AI
                data = load_data()
                user_notes = data.get("users", {}).get(lark_user_key, {}).get("notes", [])
                if not user_notes:
                    await send_lark_message(chat_id, "Bạn chưa lưu bài học nào! Hãy dùng lệnh /save để bắt đầu.")
                else:
                    parts = user_text.split()
                    target_note = user_notes[-1]
                    if len(parts) > 1:
                        try:
                            tid = int(parts[1])
                            for n in user_notes:
                                if n.get("id") == tid:
                                    target_note = n
                                    break
                        except ValueError:
                            pass
                    ai_q = await ask_ai(f"Kiến thức: '{target_note['content']}'. Đặt 2 câu hỏi Active Recall bằng tiếng Việt.")
                    await send_lark_message(chat_id, f"📚 Ôn tập bài #{target_note.get('id', '')}:\n\n{ai_q}")
            else:
                # Trò chuyện tự do với AI
                ai_reply = await ask_ai(f"Người dùng nhắn: '{user_text}'. Trả lời bằng tiếng Việt thân thiện, súc tích.")
                await send_lark_message(chat_id, ai_reply)

    return web.json_response({"code": 0, "msg": "success"})


# =====================================================================
# TELEGRAM HANDLERS
# =====================================================================

async def tg_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = process_user_action(str(update.effective_chat.id), update.effective_user.first_name, "/start")
    await update.message.reply_text(reply, parse_mode="Markdown")

async def tg_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_MESSAGE, parse_mode="Markdown")

async def tg_save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "/save " + (" ".join(context.args) if context.args else "")
    reply = process_user_action(str(update.effective_chat.id), update.effective_user.first_name, text)
    await update.message.reply_text(reply, parse_mode="Markdown")

async def tg_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = process_user_action(str(update.effective_chat.id), update.effective_user.first_name, "/list")
    await update.message.reply_text(reply, parse_mode="Markdown")

async def tg_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "/search " + (" ".join(context.args) if context.args else "")
    reply = process_user_action(str(update.effective_chat.id), update.effective_user.first_name, text)
    await update.message.reply_text(reply, parse_mode="Markdown")

async def tg_settime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "/settime " + (" ".join(context.args) if context.args else "")
    reply = process_user_action(str(update.effective_chat.id), update.effective_user.first_name, text)
    await update.message.reply_text(reply, parse_mode="Markdown")

async def tg_ontap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    notes = data.get("users", {}).get(chat_id, {}).get("notes", [])
    if not notes:
        await update.message.reply_text("Bạn chưa lưu bài học nào! Hãy dùng lệnh `/save <nội dung>` để lưu bài đầu tiên.")
        return

    target_note = notes[-1]
    if context.args:
        try:
            tid = int(context.args[0])
            for n in notes:
                if n.get("id") == tid:
                    target_note = n
                    break
        except ValueError:
            pass

    await update.message.reply_text("🤖 Groq AI đang soạn câu hỏi gợi nhớ...")
    prompt = f"Kiến thức: '{target_note['content']}'. Đặt 1 câu tóm tắt và 2 câu hỏi tình huống thực tế bằng tiếng Việt."
    ai_q = await ask_ai(prompt)
    await update.message.reply_text(f"📚 **Ôn tập bài [#{target_note.get('id', '')}]:**\n\n{ai_q}")

async def tg_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    ai_reply = await ask_ai(f"Người dùng nhắn: '{update.message.text}'. Trả lời bằng tiếng Việt thân thiện, súc tích.")
    await update.message.reply_text(ai_reply)


# =====================================================================
# BỘ NHẮC LỊCH CHỦ ĐỘNG & KHỞI CHẠY HỆ THỐNG
# =====================================================================

async def dynamic_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(LOCAL_TZ)
    current_time_str = now.strftime("%H:%M")
    today_str = now.date().isoformat()

    data = load_data()
    updated = False

    for user_key, user_data in data.get("users", {}).items():
        schedule = user_data.get("schedule", {"morning": "10:00", "evening": "19:30", "night": "22:00"})
        last_sent = user_data.get("last_sent", {})
        notes = user_data.get("notes", [])

        is_night = (current_time_str == schedule.get("night", "22:00")) and (last_sent.get("night") != today_str)
        is_morning = (current_time_str == schedule.get("morning", "10:00")) and (last_sent.get("morning") != today_str)
        is_evening = (current_time_str == schedule.get("evening", "19:30")) and (last_sent.get("evening") != today_str)

        msg_to_send = ""
        if is_night:
            msg_to_send = "🌙 **Đến giờ tổng kết!** Hôm nay bạn học được điều gì mới? Dùng `/save [chu_kỳ] <nội dung>` để lưu nhé!"
            last_sent["night"] = today_str
            updated = True
        elif is_morning or is_evening:
            due_notes = [n for n in notes if n.get("cycle") == "hangngay" or n.get("target_date") == today_str]
            if due_notes:
                sel = due_notes[-1]
                prompt = f"Ghi chú: '{sel['content']}'. Đặt 1 câu hỏi Active Recall kiểm tra trí nhớ bằng tiếng Việt."
                ai_q = await ask_ai(prompt)
                header = "☀️ **Active Recall Sáng Nay:**" if is_morning else "🎯 **Vận Dụng Tối Nay:**"
                msg_to_send = f"{header}\n\n📖 *Bài học #{sel.get('id', '')}:* {sel['content'][:100]}...\n\n{ai_q}"
                if is_morning:
                    last_sent["morning"] = today_str
                if is_evening:
                    last_sent["evening"] = today_str
                updated = True

        if msg_to_send:
            if user_key.startswith("lark_"):
                # Gửi nhắc nhở qua Lark
                lark_chat_id = user_key.replace("lark_", "")
                await send_lark_message(lark_chat_id, msg_to_send)
            else:
                # Gửi nhắc nhở qua Telegram
                try:
                    await context.bot.send_message(chat_id=int(user_key), text=msg_to_send, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Lỗi gửi tin nhắc Telegram: {e}")

        user_data["last_sent"] = last_sent

    if updated:
        save_data(data)


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", lambda req: web.Response(text="Bot Telegram & Lark dang hoat dong 24/7!"))
    app.router.add_post("/webhook/lark", handle_lark_webhook)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Đã mở cổng {port} trên Render (kèm endpoint /webhook/lark) thành công!")


async def run_bot():
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", tg_start_command))
    application.add_handler(CommandHandler("help", tg_help_command))
    application.add_handler(CommandHandler("save", tg_save_command))
    application.add_handler(CommandHandler("ontap", tg_ontap_command))
    application.add_handler(CommandHandler("list", tg_list_command))
    application.add_handler(CommandHandler("search", tg_search_command))
    application.add_handler(CommandHandler("settime", tg_settime_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_chat_handler))

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(dynamic_scheduler_job, interval=60, first=10)

    await run_web_server()

    async with application:
        await application.start()
        await application.updater.start_polling()
        logger.info("🚀 Bot Telegram & Lark đang chạy...")
        while True:
            await asyncio.sleep(3600)


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
