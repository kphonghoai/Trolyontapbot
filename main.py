"""
Trợ lý Ôn tập Kiến thức Đa nền tảng (Telegram & Lark Suite)
Chạy đồng thời cả 2 nền tảng trên cùng 1 dịch vụ Render:
- Nhận tin nhắn từ Telegram qua Polling
- Nhận tin nhắn từ Lark Suite qua Webhook (/webhook/lark)
- Lên lịch tự động nhắc nhở 3 khung giờ (22:00, 10:00, 19:30) cho cả 2 ứng dụng
"""

import os
import json
import logging
import threading
from datetime import datetime, time
from zoneinfo import ZoneInfo
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Cấu hình nhật ký
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- CẤU HÌNH KHÓA BẢO MẬT ---
# 1. Telegram Bot Token (từ @BotFather)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9217105329:AAFt7sRUrrMXfkMhL-nNbUVqDtQSsR5CvgQ")

# 2. Lark Suite Credentials (lấy từ mục Credentials & Basic Info trên Lark Developer)
LARK_APP_ID = os.getenv("LARK_APP_ID", "DÁN_LARK_APP_ID_VÀO_ĐÂY")
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "DÁN_LARK_APP_SECRET_VÀO_ĐÂY")

# Cổng Web Server cho Render (Render cấp qua biến môi trường PORT)
PORT = int(os.getenv("PORT", 8080))
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
DATA_FILE = "study_notes.json"


# --- QUẢN LÝ DỮ LIỆU CHUNG CHO CẢ 2 NỀN TẢNG ---

def load_data() -> dict:
    """Tải dữ liệu bài học đã lưu từ file JSON."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Lỗi đọc file dữ liệu: {e}")
    return {"telegram_users": {}, "lark_users": {}}


def save_data(data: dict):
    """Ghi dữ liệu bài học vào file JSON."""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Lỗi ghi file dữ liệu: {e}")


# --- CÁC HÀM XỬ LÝ LARK SUITE OPEN API ---

def get_lark_tenant_token() -> str:
    """Lấy token xác thực từ Lark Open API."""
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json; charset=utf-8"}
    payload = {"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        data = res.json()
        if data.get("code") == 0:
            return data.get("tenant_access_token", "")
        logger.error(f"Lark auth error: {data}")
    except Exception as e:
        logger.error(f"Lỗi kết nối tới Lark Auth API: {e}")
    return ""


def send_lark_message(receive_id: str, text: str, id_type: str = "open_id"):
    """Gửi tin nhắn phản hồi tới người dùng Lark."""
    token = get_lark_tenant_token()
    if not token:
        logger.error("Không lấy được token Lark để gửi tin nhắn.")
        return

    url = f"https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type={id_type}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"Lark send status: {res.status_code}, response: {res.text}")
    except Exception as e:
        logger.error(f"Lỗi gửi tin nhắn Lark: {e}")


# --- WEB SERVER XỬ LÝ LARK WEBHOOK VÀ UPTIMEROBOT ---

class WebhookHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        """Phản hồi UptimeRobot kiểm tra trạng thái hoạt động."""
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        """Phản hồi trình duyệt kiểm tra service."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Bot Ôn tập (Telegram & Lark) đang chạy 24/7!".encode("utf-8"))

    def do_POST(self):
        """Xử lý sự kiện Webhook từ Lark Suite."""
        if self.path != "/webhook/lark":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        try:
            body = json.loads(post_data.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        # 1. Xác thực Webhook URL từ Lark (URL Verification)
        if body.get("type") == "url_verification":
            challenge = body.get("challenge", "")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"challenge": challenge}).encode("utf-8"))
            return

        # Phản hồi 200 OK ngay lập tức cho Lark để không bị timeout
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "success"}).encode("utf-8"))

        # 2. Xử lý sự kiện nhận tin nhắn (im.message.receive_v1)
        header = body.get("header", {})
        if header.get("event_type") == "im.message.receive_v1":
            event = body.get("event", {})
            sender_id = event.get("sender", {}).get("sender_id", {})
            open_id = sender_id.get("open_id")
            message = event.get("message", {})

            if open_id and message.get("message_type") == "text":
                try:
                    content_json = json.loads(message.get("content", "{}"))
                    text = content_json.get("text", "").strip()
                except Exception:
                    text = ""

                # Chạy xử lý tin nhắn Lark ở luồng riêng
                threading.Thread(
                    target=process_lark_user_message,
                    args=(open_id, text),
                    daemon=True,
                ).start()


def process_lark_user_message(open_id: str, text: str):
    """Xử lý nội dung tin nhắn gửi từ Lark Suite."""
    data = load_data()
    now = datetime.now(LOCAL_TZ)

    if open_id not in data["lark_users"]:
        data["lark_users"][open_id] = {
            "registered_at": now.isoformat(),
            "notes": [],
        }

    # Nếu người dùng gửi lệnh /start hoặc lời chào
    if text.lower() in ["hi", "hello", "/start", "chào", "chao"]:
        reply = (
            "Chào bạn! 👋\n"
            "Tôi là Trợ lý Ôn tập Kiến thức trên Lark Suite.\n\n"
            "⏰ Lịch nhắc nhở mỗi ngày:\n"
            "• 22:00: Nhắc bạn gửi kiến thức mới học trong ngày.\n"
            "• 10:00 sáng hôm sau: Tóm tắt bài học + câu hỏi gợi nhớ nhanh (Active Recall).\n"
            "• 19:30 tối hôm sau: Câu hỏi thực hành & tình huống chuyên sâu.\n\n"
            "💡 Bạn có thể gửi trực tiếp bài học vào đây bất kỳ lúc nào!"
        )
        send_lark_message(open_id, reply)
        save_data(data)
        return

    # Lưu nội dung bài học mới
    data["lark_users"][open_id]["notes"].append({
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": now.isoformat(),
        "content": text,
    })
    save_data(data)

    confirm = (
        "✅ Đã ghi nhận kiến thức hôm nay trên Lark!\n\n"
        f"📝 Nội dung:\n{text}\n\n"
        "🎯 Tôi sẽ nhắc bạn ôn tập nội dung này vào 10:00 sáng và 19:30 tối mai nhé!"
    )
    send_lark_message(open_id, confirm)


# --- CÁC HÀM XỬ LÝ CHO TELEGRAM ---

async def telegram_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_name = update.effective_user.first_name

    data = load_data()
    if chat_id not in data["telegram_users"]:
        data["telegram_users"][chat_id] = {
            "name": user_name,
            "registered_at": datetime.now(LOCAL_TZ).isoformat(),
            "notes": [],
        }
        save_data(data)

    welcome = (
        f"Chào {user_name}! 👋\n\n"
        "Tôi là Trợ lý Ôn tập Kiến thức trên Telegram.\n\n"
        "⏰ Lịch hoạt động mỗi ngày:\n"
        "• 22:00: Nhắc bạn gửi kiến thức mới học hôm nay.\n"
        "• 10:00 sáng: Tóm tắt bài học + câu hỏi gợi nhớ nhanh (Active Recall).\n"
        "• 19:30 tối: Bài tập tình huống thực chiến chuyên sâu.\n\n"
        "💡 Bạn có thể gửi bài học vào đây bất kỳ lúc nào để tôi lưu lại!"
    )
    await update.message.reply_text(welcome)


async def telegram_handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    content = update.message.text
    now = datetime.now(LOCAL_TZ)

    data = load_data()
    if chat_id not in data["telegram_users"]:
        data["telegram_users"][chat_id] = {
            "name": update.effective_user.first_name,
            "registered_at": now.isoformat(),
            "notes": [],
        }

    data["telegram_users"][chat_id]["notes"].append({
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": now.isoformat(),
        "content": content,
    })
    save_data(data)

    confirm = (
        "✅ Đã ghi nhận kiến thức hôm nay trên Telegram!\n\n"
        f"📝 Nội dung:\n{content}\n\n"
        "🎯 Tôi sẽ nhắc bạn ôn tập nội dung này vào 10:00 sáng và 19:30 tối ngày mai."
    )
    await update.message.reply_text(confirm)


# --- HỆ THỐNG LÊN LỊCH NHẮC NHỞ DÙNG CHUNG CHO CẢ 2 NỀN TẢNG ---

async def job_reminder_22h(context: ContextTypes.DEFAULT_TYPE):
    """22:00: Nhắc gửi bài học mới tới cả Telegram và Lark."""
    data = load_data()
    msg = (
        "🌙 Đã 22:00 rồi!\n\n"
        "Hôm nay bạn đã học được điều gì mới và tâm đắc nhất?\n"
        "👉 Hãy gửi bài học vào đây để tôi lên lịch ôn tập cho bạn vào ngày mai nhé!"
    )
    # Gửi qua Telegram
    for chat_id in data.get("telegram_users", {}):
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=msg)
        except Exception as e:
            logger.error(f"Lỗi gửi tin nhắn 22h Telegram: {e}")

    # Gửi qua Lark Suite
    for open_id in data.get("lark_users", {}):
        send_lark_message(open_id, msg)


async def job_review_10h(context: ContextTypes.DEFAULT_TYPE):
    """10:00 sáng: Nhắc lại bài học tối qua tới cả Telegram và Lark."""
    data = load_data()

    # Nhắc Telegram
    for chat_id, info in data.get("telegram_users", {}).items():
        notes = info.get("notes", [])
        if not notes:
            continue
        content = notes[-1]["content"]
        msg = (
            "☀️ 10:00 Sáng - Kích hoạt trí nhớ chủ động (Active Recall)!\n\n"
            f"📖 Nội dung bạn đã học tối qua:\n{content}\n\n"
            "🧠 Thử thách nhanh: Không nhìn lại nội dung trên, hãy thử tóm tắt lại 3 ý chính xem sao nhé!"
        )
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=msg)
        except Exception as e:
            logger.error(f"Lỗi gửi tin nhắn 10h Telegram: {e}")

    # Nhắc Lark Suite
    for open_id, info in data.get("lark_users", {}).items():
        notes = info.get("notes", [])
        if not notes:
            continue
        content = notes[-1]["content"]
        msg = (
            "☀️ 10:00 Sáng - Kích hoạt trí nhớ chủ động (Active Recall)!\n\n"
            f"📖 Nội dung bạn đã học tối qua:\n{content}\n\n"
            "🧠 Thử thách nhanh: Không nhìn lại nội dung trên, hãy thử tóm tắt lại 3 ý chính xem sao nhé!"
        )
        send_lark_message(open_id, msg)


async def job_review_19h30(context: ContextTypes.DEFAULT_TYPE):
    """19:30 tối: Bài tập vận dụng thực chiến tới cả Telegram và Lark."""
    data = load_data()

    # Nhắc Telegram
    for chat_id, info in data.get("telegram_users", {}).items():
        notes = info.get("notes", [])
        if not notes:
            continue
        content = notes[-1]["content"]
        msg = (
            "🎯 19:30 Tối - Ôn tập chuyên sâu & Vận dụng thực tế!\n\n"
            f"Về chủ đề hôm nay bạn đã học:\n{content[:200]}...\n\n"
            "💡 Câu hỏi tình huống: Nếu áp dụng kiến thức này vào công việc thực tế ngay ngày mai, "
            "bước đầu tiên bạn sẽ làm là gì?"
        )
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=msg)
        except Exception as e:
            logger.error(f"Lỗi gửi tin nhắn 19h30 Telegram: {e}")

    # Nhắc Lark Suite
    for open_id, info in data.get("lark_users", {}).items():
        notes = info.get("notes", [])
        if not notes:
            continue
        content = notes[-1]["content"]
        msg = (
            "🎯 19:30 Tối - Ôn tập chuyên sâu & Vận dụng thực tế!\n\n"
            f"Về chủ đề hôm nay bạn đã học:\n{content[:200]}...\n\n"
            "💡 Câu hỏi tình huống: Nếu áp dụng kiến thức này vào công việc thực tế ngay ngày mai, "
            "bước đầu tiên bạn sẽ làm là gì?"
        )
        send_lark_message(open_id, msg)


# --- HÀM KHỞI ĐỘNG CHÍNH ---

def run_web_server():
    """Khởi động Web server lắng nghe webhook Lark và giữ kết nối Render."""
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    logger.info(f"Web server đang lắng nghe trên cổng {PORT}...")
    server.serve_forever()


def main():
    # 1. Khởi chạy Web Server ở luồng ngầm (Background thread)
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    # 2. Khởi tạo ứng dụng Telegram Bot ở luồng chính
    logger.info("Đang khởi tạo Telegram Bot...")
    tg_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", telegram_start))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_handle_message))

    # 3. Lên lịch định kỳ tự động 22:00, 10:00, 19:30
    jq = tg_app.job_queue
    if jq:
        jq.run_daily(job_reminder_22h, time=time(hour=22, minute=0, tzinfo=LOCAL_TZ))
        jq.run_daily(job_review_10h, time=time(hour=10, minute=0, tzinfo=LOCAL_TZ))
        jq.run_daily(job_review_19h30, time=time(hour=19, minute=30, tzinfo=LOCAL_TZ))
        logger.info("Đã lên lịch thành công cho 10:00, 19:30 và 22:00!")

    print("🚀 Bot ĐA NỀN TẢNG (Telegram & Lark Suite) đang chạy 24/7 trên Render!")
    tg_app.run_polling()


if __name__ == "__main__":
    main()
