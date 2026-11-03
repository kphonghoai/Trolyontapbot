"""
Trợ lý Ôn tập & Nhắc nhở Kiến thức Thông minh (Groq AI) - Phiên bản Tối ưu & Chuẩn hóa
- Quản lý ClientSession và Async Lock an toàn chống xung đột đa nền tảng
- Cache Lark Tenant Token tự động
- Thuật toán Spaced Repetition bậc thang (1 - 3 - 7 - 14 - 30 ngày)
- Tương tác nút bấm Inline Keyboard & Lark Card
"""

import json
import logging
import os
import io
import base64
import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from aiohttp import web, ClientSession
import pypdf
import docx
import openpyxl

# --- CẤU HÌNH NHẬT KÝ ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- CẤU HÌNH BIẾN MÔI TRƯỜNG ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
LARK_APP_ID = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")

LOCAL_TZ = timezone(timedelta(hours=7))
DATA_FILE = "study_notes.json"

# Chu kỳ lặp lại ngắt quãng theo số ngày (Spaced Repetition Stages)
SPACED_INTERVALS = [1, 3, 7, 14, 30]

# --- QUẢN LÝ TÀI NGUYÊN TOÀN CỤC ---
GLOBAL_SESSION: Optional[ClientSession] = None
FILE_LOCK = asyncio.Lock()

# Cache Lark Token: {"token": str, "expires_at": float}
LARK_TOKEN_CACHE = {"token": "", "expires_at": 0.0}


# --- QUẢN LÝ DỮ LIỆU AN TOÀN (ASYNC LOCK) ---

async def load_data() -> dict:
    default_structure = {"users": {}, "lark_users": {}}
    async with FILE_LOCK:
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in default_structure.items():
                        if k not in data:
                            data[k] = v
                    return data
            except Exception as e:
                logger.error(f"Lỗi đọc file JSON: {e}")
        return default_structure


async def save_data(data: dict):
    async with FILE_LOCK:
        try:
            # Ghi an toàn thông qua file tạm để tránh corrupt khi ngắt điện/crash
            temp_file = f"{DATA_FILE}.tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, DATA_FILE)
        except Exception as e:
            logger.error(f"Lỗi ghi file JSON: {e}")


# --- KẾT NỐI GROQ AI ---

async def ask_ai(prompt: str) -> str:
    """Truy vấn Groq AI thông qua GLOBAL_SESSION dùng chung."""
    if not GROQ_API_KEY:
        return "⚠️ Chưa cấu hình GROQ_API_KEY."

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    url = "https://api.groq.com/openai/v1/chat/completions"

    candidate_models = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "qwen/qwen3.8-27b",
    ]

    last_error = ""
    for model in candidate_models:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Bạn là trợ lý học tập và ôn tập kiến thức chuyên nghiệp. "
                        "Hãy tóm tắt và đặt câu hỏi ôn tập Active Recall bằng tiếng Việt súc tích, gắn với thực tế."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.6,
        }
        try:
            async with GLOBAL_SESSION.post(url, json=payload, headers=headers, timeout=25) as resp:
                data = await resp.json()
                if resp.status == 200:
                    choices = data.get("choices", [])
                    if choices:
                        return choices[0].get("message", {}).get("content", "").strip()
                else:
                    err_msg = data.get("error", {}).get("message", str(data))
                    last_error = f"Model {model} ({resp.status}): {err_msg}"
        except Exception as e:
            last_error = f"Lỗi kết nối Groq với model {model}: {e}"

    return f"⚠️ Lỗi kết nối Groq AI: {last_error}"


async def ask_ai_vision(image_bytes: bytes, prompt: str, mime_type: str = "image/jpeg") -> str:
    if not GROQ_API_KEY:
        return "⚠️ Chưa cấu hình GROQ_API_KEY."

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
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_img}"}},
                ],
            }
        ],
        "temperature": 0.4,
    }

    try:
        async with GLOBAL_SESSION.post(
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
            return f"Lỗi phân tích ảnh: {data.get('error', {}).get('message', 'Không có kết quả')}"
    except Exception as e:
        return f"Lỗi kết nối Vision: {e}"


# --- BỘ ĐỌC TÀI LIỆU VĂN PHÒNG ---

def extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        pages_text = [page.extract_text() for page in reader.pages[:20] if page.extract_text()]
        return "\n".join(pages_text).strip()
    except Exception as e:
        logger.error(f"Lỗi đọc PDF: {e}")
        return ""


def extract_text_from_docx(file_bytes: bytes) -> str:
    try:
        doc = docx.Document(io.BytesIO(file_bytes))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables[:5]:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    paragraphs.append(" | ".join(cells))
        return "\n".join(paragraphs).strip()
    except Exception as e:
        logger.error(f"Lỗi đọc Word: {e}")
        return ""


def extract_text_from_excel(file_bytes: bytes) -> str:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        lines = []
        for sheet_name in wb.sheetnames[:3]:
            sheet = wb[sheet_name]
            lines.append(f"--- Sheet: {sheet_name} ---")
            for row in sheet.iter_rows(max_row=35, values_only=True):
                row_vals = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
                if row_vals:
                    lines.append(" | ".join(row_vals))
        return "\n".join(lines).strip()
    except Exception as e:
        logger.error(f"Lỗi đọc Excel: {e}")
        return ""


async def process_document_content(file_name: str, text_content: str) -> str:
    if not text_content:
        return f"⚠️ Không đọc được nội dung văn bản từ `{file_name}` (file scan ảnh hoặc có mật khẩu)."

    truncated_text = text_content[:4500]
    prompt = (
        f"Dưới đây là tài liệu '{file_name}':\n\n{truncated_text}\n\n"
        "Đóng vai trợ lý học tập chuyên nghiệp, hãy trình bày ngắn gọn:\n"
        "1. 📌 **Tóm tắt cốt lõi**: 3-5 ý chính quan trọng nhất.\n"
        "2. 🧠 **Thử thách Active Recall**: 2 câu hỏi tình huống thực tế để kiểm tra khả năng vận dụng.\n"
        "3. 💡 **Hành động đề xuất**: 1 việc cần áp dụng ngay."
    )
    ai_reply = await ask_ai(prompt)
    return (
        f"📄 **KẾT QUẢ ĐỌC TÀI LIỆU:** `{file_name}`\n\n"
        f"{ai_reply}\n\n"
        f"*(💡 Mẹo: Nhập `/save 3ngay Tóm tắt {file_name}` để lưu bài học vào lộ trình nhắc)*"
    )


# --- BẢNG TRỢ GIÚP ---

HELP_MESSAGE = (
    "📖 **HỆ THỐNG LỆNH TRỢ LÝ HỌC TẬP**\n\n"
    "📝 **1. LƯU BÀI HỌC VÀ CHU KỲ NHẮC LẶP:**\n"
    "• `/save <nội dung>`: Lưu bài theo chu kỳ chuẩn Spaced Repetition (1 - 3 - 7 - 14 - 30 ngày)\n"
    "• `/save trongngay <nội dung>`: Nhắc kiểm tra ngay tối nay\n"
    "• `/save 3ngay <nội dung>`: Nhắc kiểm tra sau 3 ngày\n"
    "• `/save hangngay <nội dung>`: Nhắc đều đặn mỗi ngày\n\n"
    "📎 **2. ĐỌC TÀI LIỆU & HÌNH ẢNH:**\n"
    "• Gửi file **Word (.docx)**, **Excel (.xlsx)** hoặc **PDF (.pdf)** để bot tự tóm tắt\n"
    "• Gửi **Ảnh chụp tài liệu/sách/bảng biểu** để OCR và tạo câu hỏi tự kiểm tra\n\n"
    "🔍 **3. TRA CỨU & ÔN TẬP:**\n"
    "• `/list`: Xem danh sách các bài học đang trong lộ trình\n"
    "• `/search <từ khóa>`: Tìm kiếm lại bài học đã lưu\n"
    "• `/ontap` hoặc `/ontap <id>`: Ôn tập ngay tức thì với AI\n\n"
    "⚙️ **4. CÀI ĐẶT THỜI GIAN:**\n"
    "• `/settime <giờ_sáng> <giờ_tối> <giờ_nhắc>` (Ví dụ: `/settime 09:00 19:30 22:00`)"
)


# --- QUẢN LÝ USER VÀ THUẬT TOÁN SPACED REPETITION ---

def init_user_container(user_data_dict: dict, user_id: str, default_name: str = "Phong"):
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
        return "⚠️ Cú pháp: `/save [chu_kỳ] <nội dung>`\nVí dụ: `/save 3ngay Quy trình giao nhận`"

    now = datetime.now(LOCAL_TZ)
    today_date = now.date()

    first_arg = args[0].lower().replace("-", "").replace("_", "")
    mode_map = {
        "hangngay": ("hangngay", 0, "Hằng ngày"),
        "trongngay": ("trongngay", 0, "Hôm nay"),
        "1ngay": ("spaced", 0, "Sau 1 ngày"),
        "homsau": ("spaced", 0, "Sau 1 ngày"),
        "2ngay": ("custom", 2, "Sau 2 ngày"),
        "3ngay": ("custom", 3, "Sau 3 ngày"),
        "7ngay": ("custom", 7, "Sau 7 ngày"),
    }

    if first_arg in mode_map:
        cycle_type, offset_days, cycle_label = mode_map[first_arg]
        content = " ".join(args[1:]).strip()
    else:
        # Mặc định theo lộ trình lặp lại ngắt quãng (Spaced Repetition)
        cycle_type, offset_days, cycle_label = ("spaced", 0, "Lộ trình ghi nhớ bền vững (1-3-7-14-30 ngày)")
        content = " ".join(args).strip()

    if not content:
        return "⚠️ Bạn chưa nhập nội dung kiến thức cần ghi nhớ!"

    if cycle_type == "spaced":
        target_date = today_date + timedelta(days=SPACED_INTERVALS[0])
    elif cycle_type == "custom":
        target_date = today_date + timedelta(days=offset_days)
    else:
        target_date = today_date

    note_id = len(user_dict["notes"]) + 1
    new_note = {
        "id": note_id,
        "date": now.strftime("%Y-%m-%d %H:%M"),
        "created_date": today_date.isoformat(),
        "target_date": target_date.isoformat(),
        "cycle": cycle_type,
        "stage": 0,  # Bậc Spaced Repetition hiện tại
        "cycle_label": cycle_label,
        "content": content,
        "review_count": 0,
    }
    user_dict["notes"].append(new_note)

    return (
        f"✅ **Đã ghi nhận kiến thức [Mã #{note_id}]!**\n\n"
        f"📝 *Nội dung:* {content}\n"
        f"⏰ *Chế độ:* **{cycle_label}**\n"
        f"📅 *Lần ôn tiếp theo:* `{target_date.strftime('%d/%m/%Y')}`\n\n"
        f"💡 Ôn ngay: `/ontap {note_id}`"
    )


def update_note_progress(user_dict: dict, note_id: int, remembered: bool) -> str:
    """Cập nhật bậc Spaced Repetition khi người dùng phản hồi đã thuộc hay chưa."""
    today = datetime.now(LOCAL_TZ).date()
    target_note = None
    for n in user_dict.get("notes", []):
        if n.get("id") == note_id:
            target_note = n
            break

    if not target_note:
        return "Không tìm thấy ghi chú."

    target_note["review_count"] = target_note.get("review_count", 0) + 1

    if target_note.get("cycle") == "hangngay":
        return f"✅ Đã ghi nhận ôn tập bài #{note_id}. Bài này vẫn sẽ nhắc hằng ngày."

    if remembered:
        current_stage = target_note.get("stage", 0)
        next_stage = current_stage + 1
        if next_stage < len(SPACED_INTERVALS):
            target_note["stage"] = next_stage
            next_days = SPACED_INTERVALS[next_stage]
            next_date = today + timedelta(days=next_days)
            target_note["target_date"] = next_date.isoformat()
            return f"🎉 Tuyệt vời! Bài #{note_id} đã nâng lên cấp {next_stage + 1}. Lần ôn kế tiếp sau {next_days} ngày ({next_date.strftime('%d/%m/%Y')})."
        else:
            target_note["target_date"] = None
            return f"🏆 Xuất sắc! Bạn đã hoàn thành toàn bộ lộ trình ghi nhớ bền vững cho bài #{note_id}!"
    else:
        # Nếu quên: lùi về bậc 0 (nhắc lại vào ngày mai)
        target_note["stage"] = 0
        next_date = today + timedelta(days=1)
        target_note["target_date"] = next_date.isoformat()
        return f"💪 Đừng nản lòng! Bài #{note_id} sẽ được đặt lại lịch ôn vào ngày mai ({next_date.strftime('%d/%m/%Y')}) để củng cố trí nhớ."


async def core_ontap(user_dict: dict, arg_id: str) -> (str, Optional[int]):
    notes = user_dict.get("notes", [])
    if not notes:
        return "Bạn chưa có bài học nào. Dùng lệnh `/save <nội dung>` để lưu bài mới.", None

    target_note = None
    if arg_id.strip():
        try:
            t_id = int(arg_id.strip())
            for n in notes:
                if n.get("id") == t_id:
                    target_note = n
                    break
            if not target_note:
                return f"❌ Không tìm thấy bài học #{t_id}.", None
        except ValueError:
            return "⚠️ Mã bài học phải là chữ số.", None
    else:
        target_note = notes[-1]

    prompt = (
        f"Ghi chú kiến thức: '{target_note['content']}'.\n"
        "Hãy tóm tắt 1 câu cốt lõi ngắn, sau đó đặt 2 câu hỏi gợi mở Active Recall bằng tiếng Việt "
        "để người học tự kiểm tra xem mình đã hiểu sâu hay chưa."
    )
    ai_question = await ask_ai(prompt)
    reply = (
        f"📚 **Ôn tập bài [#{target_note['id']}] (Lưu ngày: {target_note['date']}):**\n\n"
        f"{ai_question}"
    )
    return reply, target_note["id"]


def core_list_notes(user_dict: dict) -> str:
    notes = user_dict.get("notes", [])
    if not notes:
        return "Danh sách học tập đang trống. Hãy dùng `/save` để thêm bài!"

    reply = f"📚 **Danh sách {len(notes)} bài học đã lưu:**\n\n"
    for item in notes[-10:]:
        n_id = item.get("id")
        t_date = item.get("target_date") or "Đã thuộc bền vững"
        reply += f"🔹 **[#{n_id}]** (Kế hoạch: `{t_date}`)\n👉 {item['content'][:100]}...\n\n"
    return reply


def core_search_notes(user_dict: dict, keyword: str) -> str:
    kw = keyword.strip().lower()
    if not kw:
        return "Vui lòng nhập từ khóa: `/search <từ khóa>`"

    notes = user_dict.get("notes", [])
    matched = [n for n in notes if kw in n["content"].lower()]
    if not matched:
        return f"🔍 Không tìm thấy kết quả nào với từ khóa '{keyword}'."

    reply = f"🔍 **Tìm thấy {len(matched)} kết quả:**\n\n"
    for item in matched[:5]:
        reply += f"📌 **[#{item['id']}]** `({item['date']})`:\n{item['content']}\n\n"
    return reply


def core_settime(user_dict: dict, args_list: list) -> str:
    if not args_list or len(args_list) != 3:
        return "⚠️ Nhập đủ 3 khung giờ HH:MM. Ví dụ: `/settime 09:00 19:30 22:00`"

    for t_str in args_list:
        try:
            h, m = map(int, t_str.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
        except Exception:
            return f"❌ Định dạng giờ `{t_str}` không hợp lệ."

    user_dict["schedule"] = {
        "morning": args_list[0],
        "evening": args_list[1],
        "night": args_list[2],
    }
    return f"✅ Lịch nhắc mới: ☀️ Sáng **{args_list[0]}** | 🎯 Tối **{args_list[1]}** | 🌙 Đêm **{args_list[2]}**"


# --- GIAO DIỆN NÚT BẤM (TELEGRAM INLINE KEYBOARD) ---

def make_recall_keyboard(note_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("✅ Đã nhớ rõ", callback_data=f"recall_ok_{note_id}"),
            InlineKeyboardButton("🔄 Cần ôn lại", callback_data=f"recall_retry_{note_id}"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)


# --- TELEGRAM HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_name = update.effective_user.first_name or "Phong"

    data = await load_data()
    init_user_container(data["users"], chat_id, user_name)
    await save_data(data)

    welcome_text = (
        f"Chào {user_name}! 🌟✨ Chúc bạn tràn đầy năng lượng và ngập tràn cảm hứng học tập! 🚀🔥\n\n"
        f"Tôi là **Trợ lý Ôn tập & Đồng hành Tri thức**. Hãy gửi ghi chú, tài liệu Word/Excel/PDF hoặc ảnh chụp vào đây, "
        f"tôi sẽ hỗ trợ bạn thiết lập quy trình Active Recall & Spaced Repetition hiệu quả nhất! 📚💡"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")
    await update.message.reply_text(HELP_MESSAGE, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_MESSAGE, parse_mode="Markdown")


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = await load_data()
    init_user_container(data["users"], chat_id, update.effective_user.first_name or "Phong")

    args_text = " ".join(context.args) if context.args else ""
    res = core_save_note(data["users"][chat_id], args_text)
    await save_data(data)
    await update.message.reply_text(res, parse_mode="Markdown")


async def ontap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = await load_data()
    init_user_container(data["users"], chat_id, update.effective_user.first_name or "Phong")

    arg_id = context.args[0] if context.args else ""
    res, note_id = await core_ontap(data["users"][chat_id], arg_id)

    if note_id:
        await update.message.reply_text(res, reply_markup=make_recall_keyboard(note_id))
    else:
        await update.message.reply_text(res)


async def list_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = await load_data()
    init_user_container(data["users"], chat_id, update.effective_user.first_name or "Phong")
    await update.message.reply_text(core_list_notes(data["users"][chat_id]), parse_mode="Markdown")


async def search_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = await load_data()
    init_user_container(data["users"], chat_id, update.effective_user.first_name or "Phong")
    kw = " ".join(context.args).strip() if context.args else ""
    await update.message.reply_text(core_search_notes(data["users"][chat_id], kw), parse_mode="Markdown")


async def settime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = await load_data()
    init_user_container(data["users"], chat_id, update.effective_user.first_name or "Phong")
    res = core_settime(data["users"][chat_id], context.args)
    await save_data(data)
    await update.message.reply_text(res, parse_mode="Markdown")


async def telegram_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_str = query.data

    chat_id = str(update.effective_chat.id)
    data = await load_data()
    user_dict = data["users"].get(chat_id)

    if not user_dict:
        return

    if data_str.startswith("recall_ok_"):
        n_id = int(data_str.split("_")[-1])
        msg = update_note_progress(user_dict, n_id, remembered=True)
        await save_data(data)
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=int(chat_id), text=msg)

    elif data_str.startswith("recall_retry_"):
        n_id = int(data_str.split("_")[-1])
        msg = update_note_progress(user_dict, n_id, remembered=False)
        await save_data(data)
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=int(chat_id), text=msg)


async def handle_chat_with_groq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    raw_name = update.effective_user.first_name or "Phong"
    call_name = "Phong" if "phong" in raw_name.lower() or raw_name in ["Bạn", "User"] else raw_name

    greetings = ["hi", "hello", "xin chào", "chào", "chao", "alo", "start", "/start"]
    if user_text.lower() in greetings:
        await update.message.reply_text(
            f"Chào {call_name}! 🌟 Hôm nay bạn muốn tìm hiểu hay ôn tập nội dung gì cùng mình? Cùng bắt đầu nhé! 🚀",
            parse_mode="Markdown",
        )
        return

    await update.message.chat.send_action("typing")
    prompt = (
        f"Bạn là người bạn trợ lý học tập thân thiết của anh {call_name}.\n"
        f"Yêu cầu:\n- Xưng hô gần gũi, gọi tên '{call_name}'.\n"
        f"- Không nói 'Chào bạn' khô khan.\n"
        f"- Dùng emoji tích cực (🌟, 🚀, 💡).\n- Trả lời bằng tiếng Việt tự nhiên, súc tích.\n\n"
        f"Nội dung: '{user_text}'"
    )
    reply = await ask_ai(prompt)
    await update.message.reply_text(reply)


async def telegram_handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    file_name = doc.file_name or "document"
    ext = file_name.split(".")[-1].lower() if "." in file_name else ""

    status_msg = await update.message.reply_text(f"📥 Đang tải và xử lý file `{file_name}`...")
    try:
        tg_file = await doc.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        file_bytes = buf.getvalue()

        if ext == "pdf":
            txt = extract_text_from_pdf(file_bytes)
        elif ext in ["docx", "doc"]:
            txt = extract_text_from_docx(file_bytes)
        elif ext in ["xlsx", "xls"]:
            txt = extract_text_from_excel(file_bytes)
        elif ext in ["png", "jpg", "jpeg", "webp"]:
            analysis = await ask_ai_vision(file_bytes, "Đọc chữ OCR và đặt 2 câu hỏi Active Recall.")
            await status_msg.edit_text(f"🖼️ **PHÂN TÍCH HÌNH ẢNH:**\n\n{analysis}")
            return
        else:
            await status_msg.edit_text(f"⚠️ Chưa hỗ trợ định dạng `.{ext}`.")
            return

        res = await process_document_content(file_name, txt)
        await status_msg.edit_text(res)
    except Exception as e:
        logger.error(f"Lỗi đọc file Telegram: {e}")
        await status_msg.edit_text(f"❌ Lỗi xử lý file `{file_name}`: {e}")


async def telegram_handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("📸 Đang nhận diện tài liệu qua Groq Vision AI...")
    try:
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        img_bytes = buf.getvalue()

        analysis = await ask_ai_vision(
            img_bytes,
            "1. Nhận diện chữ (OCR).\n2. Tóm tắt 3 ý quan trọng nhất.\n3. Đặt 2 câu hỏi Active Recall.",
        )
        await status_msg.edit_text(f"🖼️ **KẾT QUẢ ĐỌC HÌNH ẢNH:**\n\n{analysis}")
    except Exception as e:
        logger.error(f"Lỗi ảnh Telegram: {e}")
        await status_msg.edit_text(f"❌ Lỗi phân tích ảnh: {e}")


# --- KẾT NỐI VÀ XỬ LÝ LARK SUITE (TOKEN CACHING & INTERACTIVE CARD) ---

async def get_lark_tenant_token() -> str:
    """Lấy token Lark có áp dụng cache tránh gọi API thừa thãi."""
    global LARK_TOKEN_CACHE
    now = time.time()
    if LARK_TOKEN_CACHE["token"] and now < LARK_TOKEN_CACHE["expires_at"]:
        return LARK_TOKEN_CACHE["token"]

    if not LARK_APP_ID or not LARK_APP_SECRET:
        return ""

    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json; charset=utf-8"}
    payload = {"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}

    try:
        async with GLOBAL_SESSION.post(url, headers=headers, json=payload, timeout=10) as resp:
            data = await resp.json()
            if data.get("code") == 0:
                token = data.get("tenant_access_token", "")
                expire_seconds = data.get("expire", 7100)
                LARK_TOKEN_CACHE = {"token": token, "expires_at": now + expire_seconds - 300}
                return token
            logger.error(f"Lark auth error: {data}")
    except Exception as e:
        logger.error(f"Lỗi kết nối auth Lark: {e}")
    return ""


async def send_lark_message(open_id: str, text: str):
    token = await get_lark_tenant_token()
    if not token:
        return

    url = "https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    payload = {
        "receive_id": open_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    try:
        async with GLOBAL_SESSION.post(url, headers=headers, json=payload, timeout=10) as resp:
            if resp.status != 200:
                logger.error(f"Lỗi gửi Lark msg: {resp.status}")
    except Exception as e:
        logger.error(f"Lỗi mạng khi gửi tin Lark: {e}")


async def send_lark_interactive_card(open_id: str, title: str, content: str, note_id: int):
    """Gửi Interactive Card trên Lark với 2 nút tương tác phản hồi kết quả ôn tập."""
    token = await get_lark_tenant_token()
    if not token:
        return

    card_content = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": content},
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "✅ Đã nhớ rõ"},
                        "type": "primary",
                        "value": {"action": "recall_ok", "note_id": note_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🔄 Cần ôn lại"},
                        "type": "default",
                        "value": {"action": "recall_retry", "note_id": note_id},
                    },
                ],
            },
        ],
    }

    url = "https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    payload = {
        "receive_id": open_id,
        "msg_type": "interactive",
        "content": json.dumps(card_content, ensure_ascii=False),
    }
    try:
        await GLOBAL_SESSION.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Lỗi gửi thẻ tương tác Lark: {e}")


async def download_lark_file(message_id: str, file_key: str, resource_type: str = "file") -> bytes:
    token = await get_lark_tenant_token()
    if not token:
        return b""
    url = f"https://open.larksuite.com/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type={resource_type}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with GLOBAL_SESSION.get(url, headers=headers, timeout=30) as resp:
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

    # Xác thực Webhook Challenge từ Lark Console
    if body.get("type") == "url_verification":
        return web.json_response({"challenge": body.get("challenge", "")})

    # Xử lý sự kiện tin nhắn đến
    header = body.get("header", {})
    if header.get("event_type") == "im.message.receive_v1":
        event = body.get("event", {})
        open_id = event.get("sender", {}).get("sender_id", {}).get("open_id")
        message = event.get("message", {})
        if open_id:
            asyncio.create_task(dispatch_lark_event(open_id, message))

    # Xử lý tương tác Card Actions trên Lark
    if body.get("action"):
        action_data = body.get("action", {}).get("value", {})
        act_type = action_data.get("action")
        note_id = action_data.get("note_id")
        open_id = body.get("open_id")

        if act_type and note_id and open_id:
            data = await load_data()
            u_dict = data["lark_users"].get(open_id)
            if u_dict:
                remembered = (act_type == "recall_ok")
                msg = update_note_progress(u_dict, int(note_id), remembered=remembered)
                await save_data(data)
                return web.json_response({"toast": {"type": "success", "content": msg}})

    return web.json_response({"status": "success"})


async def dispatch_lark_event(open_id: str, message: dict):
    data = await load_data()
    init_user_container(data["lark_users"], open_id, "Phong")

    msg_type = message.get("message_type")
    msg_id = message.get("message_id")
    raw_content = message.get("content", "{}")

    try:
        content_json = json.loads(raw_content)
    except Exception:
        content_json = {}

    if msg_type == "file":
        file_key = content_json.get("file_key")
        file_name = content_json.get("file_name", "document")
        ext = file_name.split(".")[-1].lower() if "." in file_name else ""

        await send_lark_message(open_id, f"📥 Đang tải và đọc tài liệu `{file_name}`...")
        file_bytes = await download_lark_file(msg_id, file_key, "file")

        txt = ""
        if ext == "pdf":
            txt = extract_text_from_pdf(file_bytes)
        elif ext in ["docx", "doc"]:
            txt = extract_text_from_docx(file_bytes)
        elif ext in ["xlsx", "xls"]:
            txt = extract_text_from_excel(file_bytes)

        analysis = await process_document_content(file_name, txt)
        await send_lark_message(open_id, analysis)
        return

    if msg_type == "image":
        image_key = content_json.get("image_key")
        await send_lark_message(open_id, "📸 Đang phân tích chữ trong ảnh qua Groq Vision AI...")
        img_bytes = await download_lark_file(msg_id, image_key, "image")
        analysis = await ask_ai_vision(img_bytes, "Đọc chữ OCR và đặt 2 câu hỏi Active Recall.")
        await send_lark_message(open_id, f"🖼️ **KẾT QUẢ ĐỌC HÌNH ẢNH:**\n\n{analysis}")
        return

    raw = content_json.get("text", "").strip()
    if not raw:
        return

    if raw.lower() in ["/start", "hi", "hello", "chào", "chao"]:
        reply = (
            "Chào Phong! 🌟 Chúc bạn một ngày tràn đầy năng lượng và cảm hứng học tập! 🚀💡\n\n"
            f"{HELP_MESSAGE}"
        )
        await send_lark_message(open_id, reply)
    elif raw.lower() in ["/help", "/menu", "/lenh"]:
        await send_lark_message(open_id, HELP_MESSAGE)
    elif raw.startswith("/save"):
        reply = core_save_note(data["lark_users"][open_id], raw[5:].strip())
        await save_data(data)
        await send_lark_message(open_id, reply)
    elif raw == "/list":
        await send_lark_message(open_id, core_list_notes(data["lark_users"][open_id]))
    elif raw.startswith("/search"):
        await send_lark_message(open_id, core_search_notes(data["lark_users"][open_id], raw[7:].strip()))
    elif raw.startswith("/ontap"):
        reply, note_id = await core_ontap(data["lark_users"][open_id], raw[6:].strip())
        if note_id:
            await send_lark_interactive_card(open_id, f"📚 Ôn tập bài #{note_id}", reply, note_id)
        else:
            await send_lark_message(open_id, reply)
    elif raw.startswith("/settime"):
        parts = raw[8:].strip().split()
        reply = core_settime(data["lark_users"][open_id], parts)
        await save_data(data)
        await send_lark_message(open_id, reply)
    else:
        prompt = (
            f"Bạn là người bạn trợ lý học tập thân thiết của anh Phong. Xưng hô tự nhiên, gọi tên Phong, "
            f"dùng emoji vui vẻ (🌟, 🚀, 💡). Trả lời ngắn gọn: '{raw}'"
        )
        reply = await ask_ai(prompt)
        await send_lark_message(open_id, reply)


# --- BỘ LẬP LỊCH TỰ ĐỘNG (DYNAMIC SCHEDULER) ---

async def dynamic_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(LOCAL_TZ)
    current_time_str = now.strftime("%H:%M")
    today_str = now.date().isoformat()

    data = await load_data()
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

            # 1. Nhắc nhở tổng kết đêm (22:00)
            if current_time_str == schedule.get("night", "22:00") and last_sent.get("night") != today_str:
                msg = (
                    "🌙 **Đến giờ tổng kết kiến thức ngày hôm nay!** 🌟✨\n\n"
                    "Hôm nay bạn học được bài học mới nào? Hãy gửi ngay cú pháp:\n"
                    "`/save [chu_kỳ] <nội dung>` hoặc gửi file tài liệu để lưu lại nhé!"
                )
                try:
                    if platform == "telegram":
                        await context.bot.send_message(chat_id=int(user_id), text=msg, parse_mode="Markdown")
                    else:
                        await send_lark_message(user_id, msg)
                    last_sent["night"] = today_str
                    updated = True
                except Exception as e:
                    logger.error(f"Lỗi gửi nhắc đêm: {e}")

            # 2. Khung giờ ôn tập Sáng (10:00) & Tối (19:30)
            is_morning = (current_time_str == schedule.get("morning", "10:00")) and (last_sent.get("morning") != today_str)
            is_evening = (current_time_str == schedule.get("evening", "19:30")) and (last_sent.get("evening") != today_str)

            if is_morning or is_evening:
                # Tìm các bài học đến hạn kiểm tra hôm nay
                due_notes = [
                    n for n in notes
                    if n.get("cycle") == "hangngay" or (n.get("target_date") and n.get("target_date") <= today_str)
                ]

                if due_notes:
                    selected_note = due_notes[-1]
                    session_name = "buổi sáng" if is_morning else "buổi tối"
                    prompt = (
                        f"Ghi chú: '{selected_note['content']}'.\n"
                        f"Hãy tạo 1 câu hỏi đánh giá nhanh Active Recall dành cho {session_name} bằng tiếng Việt."
                    )
                    ai_q = await ask_ai(prompt)
                    title = "☀️ **Active Recall Sáng Nay:** 🧠🔥" if is_morning else "🎯 **Tình Huống Vận Dụng Tối Nay:** 🚀✨"
                    body = (
                        f"{title}\n\n"
                        f"📖 *Bài học #{selected_note['id']}:* {selected_note['content'][:120]}...\n\n"
                        f"{ai_q}"
                    )

                    try:
                        n_id = selected_note["id"]
                        if platform == "telegram":
                            await context.bot.send_message(
                                chat_id=int(user_id),
                                text=body,
                                reply_markup=make_recall_keyboard(n_id),
                                parse_mode="Markdown",
                            )
                        else:
                            await send_lark_interactive_card(user_id, title, body, n_id)

                        if is_morning:
                            last_sent["morning"] = today_str
                        if is_evening:
                            last_sent["evening"] = today_str
                        updated = True
                    except Exception as e:
                        logger.error(f"Lỗi gửi nhắc định kỳ: {e}")

            user_data["last_sent"] = last_sent

    if updated:
        await save_data(data)


# --- WEB SERVER & APP LIFECYCLE ---

async def handle_ping(request):
    return web.Response(text="Trợ lý Học tập Thông minh (Telegram & Lark Suite) đang hoạt động ổn định 24/7!")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_post("/webhook/lark", handle_lark_webhook)

    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Đã mở cổng Webhook {port} thành công.")


async def setup_bot_commands(application):
    commands = [
        BotCommand("help", "Bảng hướng dẫn lệnh mẫu"),
        BotCommand("save", "Lưu bài học theo chu kỳ lặp lại ngắt quãng"),
        BotCommand("ontap", "Ôn tập kiến thức tức thì qua AI"),
        BotCommand("list", "Xem các bài học đã ghi nhận"),
        BotCommand("search", "Tìm bài học cũ theo từ khóa"),
        BotCommand("settime", "Cài đặt 3 khung giờ nhắc"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except Exception as e:
        logger.error(f"Lỗi cài đặt Telegram Bot commands: {e}")


async def run_bot():
    global GLOBAL_SESSION
    GLOBAL_SESSION = ClientSession()

    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ Chưa thiết lập biến môi trường TELEGRAM_BOT_TOKEN!")
        return

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(setup_bot_commands)
        .build()
    )

    # Đăng ký lệnh Telegram
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("lenh", help_command))
    application.add_handler(CommandHandler("save", save_command))
    application.add_handler(CommandHandler("ontap", ontap_command))
    application.add_handler(CommandHandler("list", list_notes_command))
    application.add_handler(CommandHandler("search", search_notes_command))
    application.add_handler(CommandHandler("settime", settime_command))
    application.add_handler(CallbackQueryHandler(telegram_callback_handler))

    # Đăng ký nhận file và hình ảnh
    application.add_handler(MessageHandler(filters.PHOTO, telegram_handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, telegram_handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_with_groq))

    # Job Queue kiểm tra nhắc nhở mỗi 60 giây
    if application.job_queue:
        application.job_queue.run_repeating(dynamic_scheduler_job, interval=60, first=10)

    # Khởi chạy Web Server cho Lark
    await run_web_server()

    # Vòng đời chạy Telegram Polling
    try:
        async with application:
            await application.start()
            await application.updater.start_polling()
            logger.info("🚀 Trợ lý Học tập Đa nền tảng đã sẵn sàng!")
            while True:
                await asyncio.sleep(3600)
    finally:
        if GLOBAL_SESSION and not GLOBAL_SESSION.closed:
            await GLOBAL_SESSION.close()


def main():
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot đã dừng hoạt động an toàn.")


if __name__ == "__main__":
    main()
