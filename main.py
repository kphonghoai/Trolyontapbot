"""
Trợ lý Ôn tập & Nhắc nhở Kiến thức Đa năng (Telegram & Lark Suite + Groq AI)
Tích hợp:
- Đọc và phân tích tài liệu: Word (.docx), Excel (.xlsx), PDF (.pdf)
- Nhận diện và đọc hình ảnh (OCR + Vision AI qua Groq Llama 3.2 Vision)
- Tự động tóm tắt ý chính và tạo câu hỏi gợi nhớ Active Recall từ tài liệu tải lên
- Tự động lưu nội dung cốt lõi vào danh sách bài học ôn tập
- Bộ câu lệnh Spaced Repetition: /save, /ontap, /list, /search, /settime
- Lời chào tràn đầy năng lượng và tích cực
- Hỗ trợ đồng thời cả Telegram Bot và Lark Suite Webhook
"""

import os
import io
import re
import json
import base64
import logging
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
import pypdf
import docx
import openpyxl

# Cấu hình nhật ký
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- CẤU HÌNH TOKEN VÀ KHÓA API ---
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "9217105329:AAFt7sRUrrMXfKMhL-nNbUVqDtQSsR5CvgQ"
)
GROQ_API_KEY = os.environ.get(
    "GROQ_API_KEY", "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"
)

# Khóa Lark Suite (Lấy từ Credentials & Basic Info trên Lark Developer Console)
LARK_APP_ID = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")

LOCAL_TZ = timezone(timedelta(hours=7))
DATA_FILE = "study_notes.json"


# --- QUẢN LÝ DỮ LIỆU BÀI HỌC ---

def load_data() -> dict:
    default_structure = {
        "users": {},        # Người dùng Telegram (chat_id)
        "lark_users": {}    # Người dùng Lark Suite (open_id)
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


# --- KẾT NỐI GROQ AI (VĂN BẢN & VISION HÌNH ẢNH) ---

async def ask_ai(prompt: str) -> str:
    """Tự động truy vấn model văn bản khả dụng trên Groq và trả lời."""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    async with ClientSession() as session:
        active_model = "llama-3.1-8b-instant"
        try:
            async with session.get(
                "https://api.groq.com/openai/v1/models", headers=headers, timeout=10
            ) as m_resp:
                if m_resp.status == 200:
                    m_data = await m_resp.json()
                    model_list = [
                        m["id"]
                        for m in m_data.get("data", [])
                        if "whisper" not in m["id"]
                    ]
                    for preferred in [
                        "llama-3.3-70b-versatile",
                        "llama-3.1-8b-instant",
                        "llama3-70b-8192",
                        "llama3-8b-8192",
                        "gemma2-9b-it",
                        "mixtral-8x7b-32768",
                    ]:
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
                    "content": (
                        "Bạn là trợ lý học tập và ôn tập kiến thức chuyên nghiệp. "
                        "Hãy tóm tắt và đặt câu hỏi ôn tập Active Recall bằng tiếng Việt súc tích, gợi mở tư duy thực tế."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.6,
        }

        try:
            async with session.post(
                url, json=payload, headers=headers, timeout=30
            ) as resp:
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


async def ask_ai_vision(image_bytes: bytes, prompt: str, mime_type: str = "image/jpeg") -> str:
    """Sử dụng Groq Vision AI để đọc và phân tích hình ảnh/tài liệu scan."""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    b64_img = base64.b64encode(image_bytes).decode("utf-8")
    vision_model = "llama-3.2-11b-vision-preview"

    payload = {
        "model": vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{b64_img}"
                        },
                    },
                ],
            }
        ],
        "temperature": 0.4,
    }

    async with ClientSession() as session:
        try:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=40,
            ) as resp:
                data = await resp.json()
                if resp.status == 200:
                    choices = data.get("choices", [])
                    if choices:
                        return choices[0].get("message", {}).get("content", "").strip()
                    return "Không nhận được phản hồi phân tích hình ảnh."
                else:
                    err_msg = data.get("error", {}).get("message", str(data))
                    logger.error(f"Lỗi Groq Vision ({resp.status}): {err_msg}")
                    return f"Lỗi Groq Vision: {err_msg}"
        except Exception as e:
            logger.error(f"Lỗi kết nối Groq Vision: {e}")
            return f"Lỗi phân tích hình ảnh: {str(e)}"


# --- BỘ TRÍCH XUẤT VĂN BẢN TỪ FILE TÀI LIỆU ---

def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Trích xuất văn bản từ file PDF."""
    try:
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        pages_text = []
        for i, page in enumerate(reader.pages[:20]):
            txt = page.extract_text()
            if txt:
                pages_text.append(txt)
        return "\n".join(pages_text).strip()
    except Exception as e:
        logger.error(f"Lỗi đọc PDF: {e}")
        return ""


def extract_text_from_docx(file_bytes: bytes) -> str:
    """Trích xuất văn bản từ file Word (.docx)."""
    try:
        doc = docx.Document(io.BytesIO(file_bytes))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables[:5]:
            for row in table.rows:
                row_text = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if row_text:
                    paragraphs.append(" | ".join(row_text))
        return "\n".join(paragraphs).strip()
    except Exception as e:
        logger.error(f"Lỗi đọc Word: {e}")
        return ""


def extract_text_from_excel(file_bytes: bytes) -> str:
    """Trích xuất dữ liệu từ bảng tính Excel (.xlsx)."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        lines = []
        for sheet_name in wb.sheetnames[:3]:
            sheet = wb[sheet_name]
            lines.append(f"--- Sheet: {sheet_name} ---")
            for row in sheet.iter_rows(max_row=35, values_only=True):
                row_vals = [
                    str(cell).strip()
                    for cell in row
                    if cell is not None and str(cell).strip()
                ]
                if row_vals:
                    lines.append(" | ".join(row_vals))
        return "\n".join(lines).strip()
    except Exception as e:
        logger.error(f"Lỗi đọc Excel: {e}")
        return ""


async def process_document_content(file_name: str, text_content: str) -> str:
    """Gọi Groq AI phân tích tài liệu và đưa ra tóm tắt + câu hỏi Active Recall."""
    if not text_content:
        return f"⚠️ Không thể đọc được nội dung văn bản từ file `{file_name}` (hoặc file là dạng scan hình ảnh/bị khóa bảo mật)."

    truncated_text = text_content[:4500]
    prompt = (
        f"Dưới đây là nội dung trích xuất từ tài liệu '{file_name}':\n\n"
        f"{truncated_text}\n\n"
        "Đóng vai trợ lý học tập chuyên nghiệp, hãy trình bày bằng tiếng Việt rõ ràng, súc tích:\n"
        "1. 📌 **Tóm tắt cốt lõi**: Nêu 3 đến 5 ý quan trọng nhất trong tài liệu.\n"
        "2. 🧠 **Thử thách Active Recall**: Đặt 2 câu hỏi tình huống thực tế để kiểm tra và khắc sâu trí nhớ.\n"
        "3. 💡 **Hành động**: Đưa ra 1 gợi ý ứng dụng thực tế ngay ngày mai."
    )
    ai_reply = await ask_ai(prompt)
    return (
        f"📄 **KẾT QUẢ ĐỌC VÀ PHÂN TÍCH TÀI LIỆU:** `{file_name}`\n\n"
        f"{ai_reply}\n\n"
        f"*(💡 Mẹo: Bạn có thể gõ `/save 3ngay Tóm tắt {file_name}` để đưa bài này vào lịch nhắc ôn tập!)*"
    )


# --- BẢNG TRA CỨU CÂU LỆNH MẪU ---

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
    "📎 **2. ĐỌC FILE TÀI LIỆU & HÌNH ẢNH:**\n"
    "• Gửi trực tiếp file **Word (.docx)**, **Excel (.xlsx)** hoặc **PDF (.pdf)** vào chat $\\rightarrow$ Bot tự đọc và tóm tắt!\n"
    "• Gửi **Hình ảnh chụp tài liệu/sách vở** $\\rightarrow$ AI Vision tự động đọc chữ và ra câu hỏi ôn tập!\n\n"
    "🔍 **3. XEM LẠI & TÌM KIẾM BÀI CŨ:**\n"
    "• `/list`: Xem danh sách tất cả các bài đã lưu kèm mã #ID\n"
    "• `/search <từ khóa>`: Tìm kiếm bài theo nội dung (Ví dụ: `/search bưu cục`)\n\n"
    "🎯 **4. ÔN TẬP VỚI CÂU HỎI AI:**\n"
    "• `/ontap`: Ôn tập bài gần nhất vừa lưu\n"
    "• `/ontap <id>`: Ôn tập đích danh một bài cũ bất kỳ (Ví dụ: `/ontap 2`)\n\n"
    "⚙️ **5. CÀI ĐẶT THỜI GIAN NHẮC:**\n"
    "• `/settime <giờ_sáng> <giờ_tối> <giờ_nhắc>` (Ví dụ: `/settime 09:00 19:30 22:00`)\n\n"
    "💡 *Mẹo: Bạn có thể nhắn tin hỏi đáp tự do bất kỳ lúc nào!*"
)


# --- BỘ XỬ LÝ LỆNH CHUNG CHO CẢ TELEGRAM VÀ LARK SUITE ---

def init_user_container(user_data_dict: dict, user_id: str, default_name: str = "Bạn"):
    if user_id not in user_data_dict:
        user_data_dict[user_id] = {
            "name": default_name,
            "registered_at": datetime.now(LOCAL_TZ).isoformat(),
            "notes": [],
            "schedule": {"morning": "10:00", "evening": "19:30", "night": "22:00"},
            "last_sent": {},
        }


def core_save_note(user_dict: dict, args_text: str) -> str:
    args = args_text.strip().split()
    if not args:
        return (
            "⚠️ Vui lòng nhập nội dung sau lệnh `/save`.\n"
            "Cú pháp mẫu: `/save [chu_kỳ] <nội dung>`\n"
            "Ví dụ:\n`/save 3ngay Quy trình kiểm tra định kỳ bưu cục`\n\n"
            "💡 Gõ `/help` để xem đầy đủ các tùy chọn chu kỳ."
        )

    now = datetime.now(LOCAL_TZ)
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
        "7ngay": "7ngay",
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

    note_id = len(user_dict["notes"]) + 1
    new_note = {
        "id": note_id,
        "date": now.strftime("%Y-%m-%d %H:%M"),
        "created_date": today_date.isoformat(),
        "target_date": target_date.isoformat(),
        "cycle": cycle,
        "cycle_label": cycle_label,
        "content": content,
    }
    user_dict["notes"].append(new_note)

    return (
        f"✅ **Đã ghi nhận kiến thức [Mã #{note_id}]!**\n\n"
        f"📝 *Nội dung:* {content}\n"
        f"⏰ *Chu kỳ nhắc:* **{cycle_label}**\n"
        f"📅 *Lịch bắt đầu nhắc:* `{target_date.strftime('%d/%m/%Y')}`\n\n"
        f"💡 Dùng `/ontap {note_id}` để ôn lại bài này bất cứ lúc nào."
    )


async def core_ontap(user_dict: dict, arg_id: str) -> str:
    notes = user_dict.get("notes", [])
    if not notes:
        return "Bạn chưa lưu bài học nào! Hãy dùng lệnh `/save <nội dung>` để lưu bài đầu tiên."

    target_note = None
    if arg_id.strip():
        try:
            t_id = int(arg_id.strip())
            for n in notes:
                if n.get("id") == t_id:
                    target_note = n
                    break
            if not target_note:
                return f"❌ Không tìm thấy bài học có mã `#{t_id}`. Dùng `/list` để xem danh sách."
        except ValueError:
            return "⚠️ Mã bài học phải là số. Ví dụ: `/ontap 1`"
    else:
        target_note = notes[-1]

    prompt = (
        f"Dưới đây là kiến thức đã lưu: '{target_note['content']}'.\n"
        "Đóng vai trợ lý học tập Active Recall: Hãy tóm tắt 1 câu cốt lõi ngắn, "
        "sau đó đặt 2 câu hỏi tình huống thực tế để kiểm tra và khắc sâu trí nhớ. "
        "Trả lời bằng tiếng Việt ngắn gọn, gợi mở."
    )
    ai_question = await ask_ai(prompt)
    return (
        f"📚 **Ôn tập bài [#{target_note.get('id', 'Mới')}] lưu ngày {target_note['date']}:**\n\n"
        f"{ai_question}"
    )


def core_list_notes(user_dict: dict) -> str:
    notes = user_dict.get("notes", [])
    if not notes:
        return "Hiện tại bạn chưa lưu bài học nào. Hãy dùng `/save` để bắt đầu!"

    reply = f"📚 **Danh sách {len(notes)} bài học đã lưu của bạn:**\n\n"
    recent_notes = notes[-10:]
    for item in recent_notes:
        n_id = item.get("id", "-")
        c_label = item.get("cycle_label", "Hằng ngày")
        reply += f"🔹 **[#{n_id}]** `{item['date']}` *(Chu kỳ: {c_label})*\n"
        reply += f"👉 {item['content'][:120]}...\n"
        reply += f"*(Ôn bài này: `/ontap {n_id}`)*\n\n"

    if len(notes) > 10:
        reply += "💡 *Đang hiển thị 10 bài gần nhất. Dùng `/search <từ khóa>` để tìm bài cũ hơn.*"
    return reply


def core_search_notes(user_dict: dict, keyword: str) -> str:
    kw = keyword.strip().lower()
    if not kw:
        return "⚠️ Vui lòng nhập từ khóa cần tìm kiếm.\nVí dụ:\n`/search bưu cục` hoặc `/search kpi`"

    notes = user_dict.get("notes", [])
    if not notes:
        return "Kho dữ liệu học tập của bạn hiện đang trống!"

    matched = [n for n in notes if kw in n["content"].lower()]
    if not matched:
        return f"🔍 Không tìm thấy bài học nào chứa từ khóa: **{keyword}**"

    reply = f"🔍 **Tìm thấy {len(matched)} bài học chứa từ khóa '{keyword}':**\n\n"
    for item in matched[:5]:
        n_id = item.get("id", "-")
        reply += f"📌 **[#{n_id}]** `({item['date']})`:\n{item['content']}\n"
        reply += f"*(Gõ `/ontap {n_id}` để kiểm tra)*\n\n"
    return reply


def core_settime(user_dict: dict, args_list: list) -> str:
    if not args_list or len(args_list) != 3:
        return (
            "⚠️ Vui lòng nhập đủ 3 khung giờ theo định dạng HH:MM.\nVí dụ:\n`/settime 09:00 20:00 22:30`"
        )

    m_time, e_time, n_time = args_list[0], args_list[1], args_list[2]
    for t_str in [m_time, e_time, n_time]:
        try:
            parts = t_str.split(":")
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
        except Exception:
            return f"❌ Giờ `{t_str}` không hợp lệ. Vui lòng nhập định dạng 24h từ 00:00 đến 23:59."

    user_dict["schedule"] = {
        "morning": m_time,
        "evening": e_time,
        "night": n_time,
    }
    return (
        f"✅ **Đã cập nhật lịch nhắc mới!**\n\n"
        f"☀️ Sáng: **{m_time}**\n"
        f"🎯 Tối: **{e_time}**\n"
        f"🌙 Nhắc bài: **{n_time}**"
    )


# --- CÁC HÀM XỬ LÝ LỆNH CHO TELEGRAM BOT ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_MESSAGE, parse_mode="Markdown")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_name = update.effective_user.first_name or "Phong"

    data = load_data()
    init_user_container(data["users"], chat_id, user_name)
    save_data(data)

    welcome_text = (
        f"Chào {user_name}! 👋✨ Chúc bạn một ngày tràn đầy năng lượng và niềm vui học tập! 🌟🚀\n\n"
        "Tôi là **Trợ lý Ôn tập & Nhắc nhở Kiến thức Thông minh** (Groq AI) — người bạn đồng hành tiếp sức cho hành trình nâng cấp bản thân mỗi ngày của bạn! 📚💡🌱\n\n"
        "🎯 *Mỗi ngày một điều mới, tích tiểu thành đại cùng nhau nhé! Bạn có thể gửi bài học, file Word, Excel, PDF hoặc ảnh chụp tài liệu bất kỳ lúc nào để tôi cùng bạn đọc và ôn tập!*\n\n"
        "👉 Gõ lệnh **/help** để mở bảng tra cứu toàn bộ câu lệnh mẫu bất cứ lúc nào. Hôm nay bạn muốn cùng tôi khám phá hoặc ôn tập điều gì nào? Cùng bắt đầu thôi! 🎉🔥"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")
    await update.message.reply_text(HELP_MESSAGE, parse_mode="Markdown")


async def settime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    init_user_container(data["users"], chat_id, update.effective_user.first_name or "Bạn")

    res = core_settime(data["users"][chat_id], context.args)
    save_data(data)
    await update.message.reply_text(res, parse_mode="Markdown")


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    init_user_container(data["users"], chat_id, update.effective_user.first_name or "Bạn")

    args_text = " ".join(context.args) if context.args else ""
    res = core_save_note(data["users"][chat_id], args_text)
    save_data(data)
    await update.message.reply_text(res, parse_mode="Markdown")


async def ontap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    init_user_container(data["users"], chat_id, update.effective_user.first_name or "Bạn")

    await update.message.reply_text("🤖 Groq AI đang soạn câu hỏi gợi nhớ...")
    arg_id = context.args[0] if context.args else ""
    res = await core_ontap(data["users"][chat_id], arg_id)
    await update.message.reply_text(res)


async def list_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    init_user_container(data["users"], chat_id, update.effective_user.first_name or "Bạn")

    res = core_list_notes(data["users"][chat_id])
    await update.message.reply_text(res, parse_mode="Markdown")


async def search_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    init_user_container(data["users"], chat_id, update.effective_user.first_name or "Bạn")

    keyword = " ".join(context.args).strip() if context.args else ""
    res = core_search_notes(data["users"][chat_id], keyword)
    await update.message.reply_text(res, parse_mode="Markdown")


async def handle_chat_with_groq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    await update.message.chat.send_action("typing")

    prompt = (
        f"Người dùng nhắn: '{user_text}'.\n"
        "Trả lời bằng tiếng Việt thân thiện, súc tích. Nếu người dùng muốn lưu bài học, "
        "hãy hướng dẫn họ dùng lệnh /save <chu_kỳ> <nội dung> hoặc gửi file tài liệu (Word, Excel, PDF, Ảnh)."
    )
    ai_reply = await ask_ai(prompt)
    await update.message.reply_text(ai_reply)


# --- XỬ LÝ NHẬN FILE TÀI LIỆU VÀ ẢNH TRÊN TELEGRAM ---

async def telegram_handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý khi người dùng gửi file Word, Excel, PDF hoặc file ảnh dạng văn bản."""
    doc = update.message.document
    file_name = doc.file_name or "document"
    ext = file_name.split(".")[-1].lower() if "." in file_name else ""

    status_msg = await update.message.reply_text(f"📥 Đang tải và đọc nội dung file `{file_name}`...")

    try:
        tg_file = await doc.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        file_bytes = buf.getvalue()

        extracted_text = ""
        if ext == "pdf":
            extracted_text = extract_text_from_pdf(file_bytes)
        elif ext in ["docx", "doc"]:
            extracted_text = extract_text_from_docx(file_bytes)
        elif ext in ["xlsx", "xls"]:
            extracted_text = extract_text_from_excel(file_bytes)
        elif ext in ["png", "jpg", "jpeg", "webp"]:
            prompt = (
                "Hãy đọc toàn bộ chữ trong hình ảnh này (OCR), sau đó tóm tắt 3 ý cốt lõi "
                "và đặt 2 câu hỏi Active Recall bằng tiếng Việt để giúp người học nhớ lâu."
            )
            analysis = await ask_ai_vision(file_bytes, prompt, mime_type=f"image/{ext}")
            await status_msg.edit_text(f"🖼️ **PHÂN TÍCH HÌNH ẢNH TÀI LIỆU ({file_name}):**\n\n{analysis}")
            return
        else:
            await status_msg.edit_text(f"⚠️ Chưa hỗ trợ định dạng `.{ext}`. Vui lòng gửi file Word (.docx), Excel (.xlsx), PDF (.pdf) hoặc hình ảnh nhé!")
            return

        analysis = await process_document_content(file_name, extracted_text)
        await status_msg.edit_text(analysis)

    except Exception as e:
        logger.error(f"Lỗi xử lý file Telegram: {e}")
        await status_msg.edit_text(f"❌ Có lỗi khi đọc file `{file_name}`: {str(e)}")


async def telegram_handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý khi người dùng chụp hoặc gửi hình ảnh trực tiếp qua Telegram."""
    status_msg = await update.message.reply_text("📸 Đang quét chữ và phân tích hình ảnh bằng Groq Vision AI...")
    try:
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        img_bytes = buf.getvalue()

        prompt = (
            "Dưới đây là hình ảnh do người dùng gửi (có thể là trang sách, tài liệu hoặc ghi chú học tập):\n"
            "1. Hãy nhận diện và trích xuất chữ (OCR) các nội dung chính.\n"
            "2. Tóm tắt 3 ý cốt lõi quan trọng nhất.\n"
            "3. Đặt 2 câu hỏi tình huống ôn tập Active Recall để người học khắc sâu kiến thức."
        )
        analysis = await ask_ai_vision(img_bytes, prompt)
        await status_msg.edit_text(f"🖼️ **KẾT QUẢ ĐỌC HÌNH ẢNH BẰNG AI:**\n\n{analysis}")
    except Exception as e:
        logger.error(f"Lỗi xử lý ảnh Telegram: {e}")
        await status_msg.edit_text(f"❌ Có lỗi khi phân tích ảnh: {str(e)}")


# --- KẾT NỐI VÀ XỬ LÝ LARK SUITE OPEN API (AIOHTTP) ---

async def get_lark_tenant_token(session: ClientSession) -> str:
    if not LARK_APP_ID or not LARK_APP_SECRET:
        return ""
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json; charset=utf-8"}
    payload = {"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}
    try:
        async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
            data = await resp.json()
            if data.get("code") == 0:
                return data.get("tenant_access_token", "")
            logger.error(f"Lark auth error: {data}")
    except Exception as e:
        logger.error(f"Lỗi lấy token Lark: {e}")
    return ""


async def send_lark_message(open_id: str, text: str):
    async with ClientSession() as session:
        token = await get_lark_tenant_token(session)
        if not token:
            logger.error("Chưa có Token Lark để gửi tin nhắn.")
            return

        url = "https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=open_id"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {
            "receive_id": open_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        try:
            async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
                logger.info(f"Lark send status: {resp.status}")
        except Exception as e:
            logger.error(f"Lỗi gửi tin nhắn Lark: {e}")


async def download_lark_file(message_id: str, file_key: str, resource_type: str = "file") -> bytes:
    """Tải file đính kèm từ Lark Suite."""
    async with ClientSession() as session:
        token = await get_lark_tenant_token(session)
        if not token:
            return b""
        url = f"https://open.larksuite.com/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type={resource_type}"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.get(url, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception as e:
            logger.error(f"Lỗi tải file Lark: {e}")
    return b""


async def handle_lark_webhook(request):
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    # Xác thực Webhook URL
    if body.get("type") == "url_verification":
        challenge = body.get("challenge", "")
        return web.json_response({"challenge": challenge})

    # Xử lý tin nhắn
    header = body.get("header", {})
    if header.get("event_type") == "im.message.receive_v1":
        event = body.get("event", {})
        open_id = event.get("sender", {}).get("sender_id", {}).get("open_id")
        message = event.get("message", {})

        if open_id:
            asyncio.create_task(dispatch_lark_event(open_id, message))

    return web.json_response({"status": "success"})


async def dispatch_lark_event(open_id: str, message: dict):
    data = load_data()
    init_user_container(data["lark_users"], open_id, "Phong")

    msg_type = message.get("message_type")
    msg_id = message.get("message_id")
    raw_content = message.get("content", "{}")

    try:
        content_json = json.loads(raw_content)
    except Exception:
        content_json = {}

    # 1. Nếu là file tài liệu (Word, Excel, PDF)
    if msg_type == "file":
        file_key = content_json.get("file_key")
        file_name = content_json.get("file_name", "document")
        ext = file_name.split(".")[-1].lower() if "." in file_name else ""

        await send_lark_message(open_id, f"📥 Đang tải và đọc tài liệu `{file_name}`...")
        file_bytes = await download_lark_file(msg_id, file_key, "file")

        extracted_text = ""
        if ext == "pdf":
            extracted_text = extract_text_from_pdf(file_bytes)
        elif ext in ["docx", "doc"]:
            extracted_text = extract_text_from_docx(file_bytes)
        elif ext in ["xlsx", "xls"]:
            extracted_text = extract_text_from_excel(file_bytes)

        analysis = await process_document_content(file_name, extracted_text)
        await send_lark_message(open_id, analysis)
        return

    # 2. Nếu là hình ảnh
    if msg_type == "image":
        image_key = content_json.get("image_key")
        await send_lark_message(open_id, "📸 Đang phân tích chữ trong hình ảnh bằng Groq Vision AI...")
        img_bytes = await download_lark_file(msg_id, image_key, "image")

        prompt = "Hãy đọc chữ trong hình ảnh (OCR), tóm tắt các ý cốt lõi và đặt 2 câu hỏi Active Recall ôn tập."
        analysis = await ask_ai_vision(img_bytes, prompt)
        await send_lark_message(open_id, f"🖼️ **KẾT QUẢ ĐỌC HÌNH ẢNH:**\n\n{analysis}")
        return

    # 3. Nếu là tin nhắn văn bản thông thường
    raw = content_json.get("text", "").strip()
    if not raw:
        return

    if raw.lower() in ["/start", "hi", "hello", "chào", "chao"]:
        reply = (
            "Chào Phong! 👋✨ Chúc bạn một ngày tràn đầy năng lượng và niềm vui học tập! 🌟🚀\n\n"
            "Tôi là **Trợ lý Ôn tập & Nhắc nhở Kiến thức Thông minh** (Groq AI) trên Lark Suite — người bạn đồng hành tiếp sức cho hành trình nâng cấp bản thân mỗi ngày của bạn! 📚💡🌱\n\n"
            "🎯 *Mỗi ngày một điều mới, tích tiểu thành đại cùng nhau nhé! Bạn có thể gửi bài học, file Word, Excel, PDF hoặc ảnh chụp tài liệu bất kỳ lúc nào để tôi cùng bạn đọc và ôn tập!*\n\n"
            "👉 Gõ lệnh **/help** để mở bảng tra cứu toàn bộ câu lệnh mẫu bất cứ lúc nào. Hôm nay bạn muốn cùng tôi khám phá hoặc ôn tập điều gì nào? Cùng bắt đầu thôi! 🎉🔥\n\n"
            f"{HELP_MESSAGE}"
        )
    elif raw.lower() in ["/help", "/menu", "/lenh", "hướng dẫn", "huong dan"]:
        reply = HELP_MESSAGE
    elif raw.startswith("/save"):
        args_text = raw[5:].strip()
        reply = core_save_note(data["lark_users"][open_id], args_text)
        save_data(data)
    elif raw == "/list":
        reply = core_list_notes(data["lark_users"][open_id])
    elif raw.startswith("/search"):
        kw = raw[7:].strip()
        reply = core_search_notes(data["lark_users"][open_id], kw)
    elif raw.startswith("/ontap"):
        arg_id = raw[6:].strip()
        reply = await core_ontap(data["lark_users"][open_id], arg_id)
    elif raw.startswith("/settime"):
        parts = raw[8:].strip().split()
        reply = core_settime(data["lark_users"][open_id], parts)
        save_data(data)
    else:
        prompt = (
            f"Người dùng nhắn: '{raw}'.\n"
            "Trả lời bằng tiếng Việt thân thiện, súc tích. Hướng dẫn họ gửi bài học hoặc gửi file tài liệu (Word, Excel, PDF, Ảnh)."
        )
        reply = await ask_ai(prompt)

    await send_lark_message(open_id, reply)


# --- ĐỊNH KỲ NHẮC NHỞ TỰ ĐỘNG (TELEGRAM & LARK SUITE) ---

async def dynamic_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(LOCAL_TZ)
    current_time_str = now.strftime("%H:%M")
    today_date = now.date()
    today_str = today_date.isoformat()

    data = load_data()
    updated = False

    platform_groups = [
        ("telegram", data.get("users", {})),
        ("lark", data.get("lark_users", {})),
    ]

    for platform, users_dict in platform_groups:
        for user_id, user_data in users_dict.items():
            schedule = user_data.get(
                "schedule", {"morning": "10:00", "evening": "19:30", "night": "22:00"}
            )
            last_sent = user_data.get("last_sent", {})
            notes = user_data.get("notes", [])

            # 1. Nhắc đêm (22:00)
            if current_time_str == schedule.get("night", "22:00"):
                if last_sent.get("night") != today_str:
                    msg = (
                        "🌙 **Đến giờ tổng kết kiến thức ngày hôm nay!** 🌟\n\n"
                        "Hôm nay bạn học được điều gì mới hoặc rút ra bài học gì? Bạn có thể gửi bài học bằng lệnh:\n"
                        "`/save [chu_kỳ] <nội dung>`\n"
                        "*(Hoặc gửi trực tiếp file Word, Excel, PDF, ảnh tài liệu vào đây)*\n\n"
                        "💡 Gõ `/help` để xem bảng câu lệnh."
                    )
                    try:
                        if platform == "telegram":
                            await context.bot.send_message(
                                chat_id=int(user_id), text=msg, parse_mode="Markdown"
                            )
                        else:
                            await send_lark_message(user_id, msg)
                        last_sent["night"] = today_str
                        updated = True
                    except Exception as e:
                        logger.error(f"Lỗi gửi tin nhắc đêm ({platform}): {e}")

            # 2. Nhắc sáng (10:00) & tối (19:30)
            is_morning = (current_time_str == schedule.get("morning", "10:00")) and (
                last_sent.get("morning") != today_str
            )
            is_evening = (current_time_str == schedule.get("evening", "19:30")) and (
                last_sent.get("evening") != today_str
            )

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
                    session_type = (
                        "buổi sáng" if is_morning else "thực chiến buổi tối"
                    )
                    prompt = (
                        f"Ghi chú đã học: '{selected_note['content']}'.\n"
                        f"Hãy đặt 1 câu hỏi đánh giá nhanh Active Recall dành cho {session_type} bằng tiếng Việt để giúp người học nhớ lâu."
                    )
                    ai_q = await ask_ai(prompt)

                    title = (
                        "☀️ **Active Recall Sáng Nay:** 🧠🔥"
                        if is_morning
                        else "🎯 **Tình Huống Vận Dụng Tối Nay:** 🚀✨"
                    )
                    msg_body = (
                        f"{title}\n\n"
                        f"📖 *Bài học #{selected_note.get('id', '')}:* {selected_note['content'][:100]}...\n\n"
                        f"{ai_q}\n\n"
                        f"💡 *(Gõ `/ontap {selected_note.get('id', '')}` để xem giải nghĩa đầy đủ)*"
                    )
                    try:
                        if platform == "telegram":
                            await context.bot.send_message(
                                chat_id=int(user_id),
                                text=msg_body,
                                parse_mode="Markdown",
                            )
                        else:
                            await send_lark_message(user_id, msg_body)

                        if is_morning:
                            last_sent["morning"] = today_str
                        if is_evening:
                            last_sent["evening"] = today_str
                        updated = True
                    except Exception as e:
                        logger.error(f"Lỗi gửi tin nhắc định kỳ ({platform}): {e}")

            user_data["last_sent"] = last_sent

    if updated:
        save_data(data)


# --- WEB SERVER AIOHTTP (HEALTH CHECK RENDER & LARK WEBHOOK) ---

async def handle_ping(request):
    return web.Response(
        text="Trợ lý Ôn tập Kiến thức (Telegram & Lark Suite + File Reading AI) đang hoạt động 24/7!"
    )


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_post("/webhook/lark", handle_lark_webhook)

    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Đã mở cổng {port} trên Render thành công!")


async def setup_bot_commands(application):
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
    # 1. Khởi tạo Telegram Bot
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

    # Xử lý nhận file tài liệu và hình ảnh
    application.add_handler(MessageHandler(filters.PHOTO, telegram_handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, telegram_handle_document))

    # Xử lý tin nhắn văn bản thông thường
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_with_groq)
    )

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(dynamic_scheduler_job, interval=60, first=10)

    # 2. Khởi chạy Web Server aiohttp
    await run_web_server()

    # 3. Chạy Polling Telegram
    async with application:
        await application.start()
        await application.updater.start_polling()
        logger.info("🚀 Bot ĐA NĂNG Đọc File & Groq AI đang hoạt động 24/7...")
        while True:
            await asyncio.sleep(3600)


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
