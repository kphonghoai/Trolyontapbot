"""
Trợ lý Ôn tập Kiến thức Toàn diện Đa nền tảng (Telegram & Lark Suite)
Tích hợp đầy đủ hệ thống câu lệnh thông minh:
- /save: Lưu bài học kèm chu kỳ lặp lại ngắt quãng (trong ngày, ngày mai, 2 ngày, 3 ngày, 7 ngày, hàng ngày)
- /list: Xem danh sách bài học kèm mã số #ID
- /search: Tìm kiếm bài học theo từ khóa
- /ontap: Ôn tập bài gần nhất hoặc bài theo ID với câu hỏi gợi nhớ chủ động (Active Recall / AI)
- /settime: Cài đặt khung giờ nhắc nhở tùy chỉnh
- Hỗ trợ đồng thời cả Telegram Bot và Lark Suite Webhook
"""

import os
import re
import json
import logging
import threading
from datetime import datetime, timedelta, time
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

# --- CẤU HÌNH KHÓA BẢO MẬT & MÔI TRƯỜNG ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9217105329:AAFt7sRUrrMXfKMhL-nNbUVqDtQSsR5CvgQ")
LARK_APP_ID = os.getenv("LARK_APP_ID", "")
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

PORT = int(os.getenv("PORT", 8080))
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
DATA_FILE = "study_notes.json"


# --- QUẢN LÝ CƠ SỞ DỮ LIỆU BÀI HỌC ---

def load_data() -> dict:
    default_structure = {
        "notes": [],
        "next_id": 1,
        "settings": {
            "morning": "10:00",
            "evening": "19:30",
            "night": "22:00"
        },
        "telegram_users": {},
        "lark_users": {}
    }
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in default_structure.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception as e:
            logger.error(f"Lỗi đọc file: {e}")
    return default_structure


def save_data(data: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Lỗi ghi file: {e}")


# --- BẢNG HƯỚNG DẪN CÂU LỆNH MẪU ---

HELP_MESSAGE = (
    "📖 **BẢNG TRA CỨU CÂU LỆNH MẪU**\n\n"
    "📝 **1. LƯU BÀI HỌC VÀ CHU KỲ NHẮC:**\n"
    "• `/save <nội dung>`: Lưu bài (nhắc chuẩn ngày mai)\n"
    "• `/save trongngay <nội dung>`: Nhắc ngay tối nay\n"
    "• `/save homsau <nội dung>`: Nhắc sau 1 ngày (ngày mai)\n"
    "• `/save 2ngay <nội dung>`: Nhắc sau 2 ngày\n"
    "• `/save 3ngay <nội dung>`: Nhắc sau 3 ngày\n"
    "• `/save 7ngay <nội dung>`: Nhắc sau 7 ngày (1 tuần)\n"
    "• `/save hangngay <nội dung>`: Nhắc bài lặp lại mỗi ngày\n"
    "_(Ví dụ: `/save 3ngay Quy trình kiểm kê định kỳ bưu cục`)_\n\n"
    "🔍 **2. XEM LẠI & TÌM KIẾM BÀI CŨ:**\n"
    "• `/list`: Xem danh sách tất cả bài đã lưu kèm mã #ID\n"
    "• `/search <từ khóa>`: Tìm kiếm bài theo nội dung\n"
    "_(Ví dụ: `/search bưu cục`)_\n\n"
    "🎯 **3. ÔN TẬP VỚI CÂU HỎI:**\n"
    "• `/ontap`: Ôn tập bài gần nhất vừa lưu\n"
    "• `/ontap <id>`: Ôn tập đích danh một bài cũ bất kỳ\n"
    "_(Ví dụ: `/ontap 2`)_\n\n"
    "⚙️ **4. CÀI ĐẶT THỜI GIAN NHẮC:**\n"
    "• `/settime <giờ_sáng> <giờ_tối> <giờ_nhắc>`\n"
    "_(Ví dụ: `/settime 10:00 19:30 22:00`)_"
)


# --- BỘ XỬ LÝ LỆNH CHUNG CHO CẢ TELEGRAM VÀ LARK ---

def handle_save(text: str) -> str:
    """Xử lý lệnh /save theo chu kỳ."""
    # Bỏ tiền tố /save
    raw = re.sub(r"^/save\s*", "", text, flags=re.IGNORECASE).strip()
    if not raw:
        return "⚠️ Vui lòng nhập nội dung bài học. Ví dụ:\n`/save 3ngay Quy trình kiểm kê định kỳ bưu cục`"

    now = datetime.now(LOCAL_TZ)
    cycle = "homsau"
    target_date = now + timedelta(days=1)
    content = raw

    # Phân tích chu kỳ
    parts = raw.split(" ", 1)
    keyword = parts[0].lower()

    cycle_map = {
        "trongngay": (0, "nhắc ngay tối nay"),
        "homsau": (1, "nhắc ngày mai"),
        "1ngay": (1, "nhắc sau 1 ngày"),
        "2ngay": (2, "nhắc sau 2 ngày"),
        "3ngay": (3, "nhắc sau 3 ngày"),
        "7ngay": (7, "nhắc sau 7 ngày"),
        "hangngay": (1, "nhắc lặp lại hàng ngày"),
    }

    if keyword in cycle_map:
        days, desc = cycle_map[keyword]
        cycle = keyword
        target_date = now + timedelta(days=days)
        content = parts[1].strip() if len(parts) > 1 else ""
    else:
        desc = "nhắc ngày mai"

    if not content:
        return f"⚠️ Bạn chưa nhập nội dung cho chu kỳ `{cycle}`. Ví dụ:\n`/save {cycle} Tóm tắt quy trình vận hành`"

    data = load_data()
    note_id = data.get("next_id", 1)
    data["next_id"] = note_id + 1

    new_note = {
        "id": note_id,
        "content": content,
        "cycle": cycle,
        "created_at": now.strftime("%Y-%m-%d %H:%M"),
        "target_date": target_date.strftime("%Y-%m-%d"),
        "reviewed_count": 0,
    }
    data["notes"].append(new_note)
    save_data(data)

    return (
        f"✅ **Đã lưu bài học #{note_id} thành công!**\n\n"
        f"📝 **Nội dung:** {content}\n"
        f"⏰ **Chu kỳ:** `{cycle}` ({desc})\n"
        f"📅 **Ngày bắt đầu nhắc:** {target_date.strftime('%d/%m/%Y')}\n\n"
        f"💡 Gõ `/ontap {note_id}` bất cứ khi nào bạn muốn ôn tập bài này!"
    )


def handle_list() -> str:
    """Xử lý lệnh /list."""
    data = load_data()
    notes = data.get("notes", [])
    if not notes:
        return "📚 Bạn chưa có bài học nào trong danh sách. Hãy dùng lệnh `/save <nội dung>` để lưu nhé!"

    res = "📚 **DANH SÁCH BÀI HỌC CỦA BẠN:**\n\n"
    for item in notes[-10:]:  # Hiện 10 bài gần nhất
        short = item["content"][:80] + ("..." if len(item["content"]) > 80 else "")
        res += f"• **#{item['id']}** `[{item.get('cycle', 'homsau')}]`: {short}\n"

    res += f"\n_Tổng cộng: {len(notes)} bài học. Dùng `/ontap <id>` để làm bài tập ôn tập!_"
    return res


def handle_search(keyword: str) -> str:
    """Xử lý lệnh /search."""
    kw = keyword.strip()
    if not kw:
        return "⚠️ Vui lòng nhập từ khóa cần tìm. Ví dụ: `/search bưu cục`"

    data = load_data()
    matches = [
        n for n in data.get("notes", [])
        if kw.lower() in n.get("content", "").lower()
    ]

    if not matches:
        return f"🔍 Không tìm thấy bài học nào chứa từ khóa `{kw}`."

    res = f"🔍 **KẾT QUẢ TÌM KIẾM CHO '{kw}':**\n\n"
    for item in matches[:5]:
        res += f"• **#{item['id']}** ({item.get('created_at', '')}):\n  {item['content']}\n\n"
    return res


def handle_ontap(param: str) -> str:
    """Xử lý lệnh /ontap với câu hỏi Active Recall."""
    data = load_data()
    notes = data.get("notes", [])
    if not notes:
        return "📚 Bạn chưa lưu bài học nào để ôn tập. Hãy lưu bài bằng `/save` trước nhé!"

    target_note = None
    if param.strip().isdigit():
        target_id = int(param.strip())
        for n in notes:
            if n.get("id") == target_id:
                target_note = n
                break
        if not target_note:
            return f"⚠️ Không tìm thấy bài học #{target_id}."
    else:
        target_note = notes[-1]

    content = target_note["content"]
    note_id = target_note["id"]

    # Tạo câu hỏi ôn tập Active Recall
    return (
        f"🎯 **BÀI TẬP ÔN TẬP #{note_id}**\n\n"
        f"📖 **Chủ đề bài học:**\n_{content}_\n\n"
        "🧠 **THỬ THÁCH GỢI NHỚ CHỦ ĐỘNG (ACTIVE RECALL):**\n"
        "1. Không nhìn lại tài liệu, hãy nêu ra **3 ý cốt lõi** quan trọng nhất của bài học này.\n"
        "2. **Tình huống thực tế:** Nếu áp dụng kiến thức này vào công việc ngày mai, bạn sẽ triển khai bước đầu tiên như thế nào?\n"
        "3. Điểm nào trong nội dung này bạn thấy dễ bị nhầm lẫn nhất và cách phòng tránh là gì?\n\n"
        "👉 _Hãy trả lời trực tiếp vào đây để tự kiểm tra mức độ ghi nhớ của mình nhé!_"
    )


def handle_settime(text: str) -> str:
    """Xử lý lệnh /settime."""
    parts = text.strip().split()
    if len(parts) < 3:
        return (
            "⚠️ Vui lòng nhập đủ 3 khung giờ theo định dạng HH:MM.\n"
            "Ví dụ: `/settime 10:00 19:30 22:00`\n"
            "(Khung giờ 1: Ôn sáng | Khung giờ 2: Ôn tối | Khung giờ 3: Nhắc nộp bài)"
        )

    m, e, n = parts[0], parts[1], parts[2]
    data = load_data()
    data["settings"]["morning"] = m
    data["settings"]["evening"] = e
    data["settings"]["night"] = n
    save_data(data)

    return (
        "⚙️ **ĐÃ CẬP NHẬT THỜI GIAN NHẮC NHỞ!**\n\n"
        f"• ☀️ Ôn tập buổi sáng: **{m}**\n"
        f"• 🎯 Ôn tập buổi tối: **{e}**\n"
        f"• 🌙 Nhắc gửi bài mới: **{n}**"
    )


def route_command(text: str) -> str:
    """Điều hướng câu lệnh văn bản bất kỳ đến đúng hàm xử lý."""
    raw = text.strip()
    if not raw:
        return ""

    if raw in ["/start", "hi", "hello", "chào", "chao"]:
        return (
            "Chào bạn! 👋 Tôi là **Trợ lý Ôn tập Kiến thức** của bạn.\n\n"
            f"{HELP_MESSAGE}"
        )

    if raw in ["/help", "/menu", "hướng dẫn", "huong dan"]:
        return HELP_MESSAGE

    if raw.startswith("/save"):
        return handle_save(raw)

    if raw == "/list":
        return handle_list()

    if raw.startswith("/search"):
        kw = re.sub(r"^/search\s*", "", raw, flags=re.IGNORECASE)
        return handle_search(kw)

    if raw.startswith("/ontap"):
        param = re.sub(r"^/ontap\s*", "", raw, flags=re.IGNORECASE)
        return handle_ontap(param)

    if raw.startswith("/settime"):
        param = re.sub(r"^/settime\s*", "", raw, flags=re.IGNORECASE)
        return handle_settime(param)

    # Nếu gửi văn bản thông thường (không có dấu /), tự động lưu như một bài học mới
    return handle_save(f"/save {raw}")


# --- KẾT NỐI LARK SUITE OPEN API ---

def get_lark_tenant_token() -> str:
    if not LARK_APP_ID or not LARK_APP_SECRET:
        return ""
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json; charset=utf-8"}
    try:
        res = requests.post(url, headers=headers, json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}, timeout=10)
        data = res.json()
        if data.get("code") == 0:
            return data.get("tenant_access_token", "")
        logger.error(f"Lark auth error: {data}")
    except Exception as e:
        logger.error(f"Lark auth exception: {e}")
    return ""


def send_lark_message(receive_id: str, text: str, id_type: str = "open_id"):
    token = get_lark_tenant_token()
    if not token:
        logger.error("Chưa có Lark Token (hãy kiểm tra LARK_APP_ID và LARK_APP_SECRET)")
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
        logger.info(f"Lark send status: {res.status_code}")
    except Exception as e:
        logger.error(f"Lỗi gửi Lark: {e}")


# --- WEB SERVER XỬ LÝ LARK WEBHOOK VÀ HEALTH CHECK ---

class WebhookHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Trợ lý Ôn tập Kiến thức (Telegram & Lark) đang hoạt động 24/7!".encode("utf-8"))

    def do_POST(self):
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

        # Xác thực URL
        if body.get("type") == "url_verification":
            challenge = body.get("challenge", "")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"challenge": challenge}).encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "success"}).encode("utf-8"))

        # Xử lý tin nhắn
        header = body.get("header", {})
        if header.get("event_type") == "im.message.receive_v1":
            event = body.get("event", {})
            open_id = event.get("sender", {}).get("sender_id", {}).get("open_id")
            message = event.get("message", {})

            if open_id and message.get("message_type") == "text":
                try:
                    text = json.loads(message.get("content", "{}")).get("text", "").strip()
                except Exception:
                    text = ""

                threading.Thread(target=process_lark_message, args=(open_id, text), daemon=True).start()


def process_lark_message(open_id: str, text: str):
    data = load_data()
    if open_id not in data["lark_users"]:
        data["lark_users"][open_id] = datetime.now(LOCAL_TZ).isoformat()
        save_data(data)

    reply = route_command(text)
    if reply:
        send_lark_message(open_id, reply)


# --- XỬ LÝ LỆNH CHO TELEGRAM ---

async def tg_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    if chat_id not in data["telegram_users"]:
        data["telegram_users"][chat_id] = update.effective_user.first_name or "Bạn"
        save_data(data)

    text = update.message.text or ""
    reply = route_command(text)
    if reply:
        await update.message.reply_text(reply, parse_mode="Markdown")


# --- ĐỊNH KỲ NHẮC NHỞ TỰ ĐỘNG ---

async def job_morning_review(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    notes = data.get("notes", [])
    if not notes:
        return
    latest = notes[-1]
    msg = (
        f"☀️ **10:00 Sáng - Kích hoạt trí nhớ chủ động!**\n\n"
        f"📖 Bài học cần ôn: _{latest['content']}_\n\n"
        f"👉 Hãy gõ `/ontap {latest['id']}` để làm câu hỏi thử thách phản xạ nhé!"
    )
    for cid in data.get("telegram_users", {}):
        try:
            await context.bot.send_message(chat_id=int(cid), text=msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Lỗi gửi sáng Telegram: {e}")
    for oid in data.get("lark_users", {}):
        send_lark_message(oid, msg)


async def job_evening_review(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    notes = data.get("notes", [])
    if not notes:
        return
    latest = notes[-1]
    msg = (
        f"🎯 **19:30 Tối - Vận dụng thực tế chuyên sâu!**\n\n"
        f"Về bài học #{latest['id']}: _{latest['content'][:150]}..._\n\n"
        "💡 **Câu hỏi tình huống:** Bạn sẽ ứng dụng kiến thức này vào công việc thực tế như thế nào?"
    )
    for cid in data.get("telegram_users", {}):
        try:
            await context.bot.send_message(chat_id=int(cid), text=msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Lỗi gửi tối Telegram: {e}")
    for oid in data.get("lark_users", {}):
        send_lark_message(oid, msg)


async def job_night_prompt(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    msg = (
        "🌙 **Đã 22:00 rồi!**\n\n"
        "Hôm nay bạn đã học được điều gì mới và tâm đắc nhất?\n"
        "👉 Hãy lưu bài theo cú pháp:\n"
        "`/save <nội dung>` (nhắc ngày mai)\n"
        "`/save 3ngay <nội dung>` (nhắc sau 3 ngày)\n"
        "để tôi lên lịch ôn tập cho bạn nhé!"
    )
    for cid in data.get("telegram_users", {}):
        try:
            await context.bot.send_message(chat_id=int(cid), text=msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Lỗi gửi đêm Telegram: {e}")
    for oid in data.get("lark_users", {}):
        send_lark_message(oid, msg)


# --- KHỞI CHẠY HỆ THỐNG ---

def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    logger.info(f"Web server đang lắng nghe trên cổng {PORT}...")
    server.serve_forever()


def main():
    # 1. Chạy Web Server cho Lark và UptimeRobot ở luồng nền
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    # 2. Khởi tạo Telegram Bot ở luồng chính
    try:
        logger.info("Đang khởi tạo Telegram Bot...")
        tg_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

        # Đăng ký nhận mọi câu lệnh và tin nhắn
        tg_app.add_handler(CommandHandler(["start", "help", "menu", "save", "list", "search", "ontap", "settime"], tg_command_handler))
        tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_command_handler))

        # Lên lịch nhắc nhở
        jq = tg_app.job_queue
        if jq:
            jq.run_daily(job_morning_review, time=time(hour=10, minute=0, tzinfo=LOCAL_TZ))
            jq.run_daily(job_evening_review, time=time(hour=19, minute=30, tzinfo=LOCAL_TZ))
            jq.run_daily(job_night_prompt, time=time(hour=22, minute=0, tzinfo=LOCAL_TZ))
            logger.info("Đã lên lịch định kỳ 10:00, 19:30 và 22:00!")

        print("🚀 Bot Đa nền tảng với đầy đủ bộ lệnh /save, /list, /search, /ontap, /settime đang chạy 24/7!")
        tg_app.run_polling()
    except Exception as e:
        logger.error(f"Lỗi khởi chạy Telegram: {e}")
        web_thread.join()


if __name__ == "__main__":
    main()
