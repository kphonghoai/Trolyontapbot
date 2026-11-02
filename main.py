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
