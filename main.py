"""
Trợ lý Ôn tập & Nhắc nhở Kiến thức Thông minh (Groq AI) - Bản Hỗ trợ Lưu & Tải lại File Gốc
- Lưu trữ vĩnh viễn trên Supabase PostgreSQL (IPv4 Pooler)
- Tự động lưu file_id của tài liệu/ảnh và hỗ trợ lệnh /getfile để tải lại file gốc
- Trích xuất dữ liệu thông minh qua Groq AI trong luồng đối thoại tự nhiên
- Chạy đồng thời Telegram Bot & Lark Suite Webhook
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

import asyncpg
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
DATABASE_URL = os.environ.get("DATABASE_URL", "")

LOCAL_TZ = timezone(timedelta(hours=7))
SPACED_INTERVALS = [1, 3, 7, 14, 30]

# --- QUẢN LÝ TÀI NGUYÊN TOÀN CỤC ---
GLOBAL_SESSION: Optional[ClientSession] = None
DB_POOL: Optional[asyncpg.Pool] = None
LARK_TOKEN_CACHE = {"token": "", "expires_at": 0.0}


# --- KẾT NỐI VÀ QUẢN LÝ DỮ LIỆU SUPABASE POSTGRESQL ---

async def init_db():
    """Khởi tạo connection pool tới Supabase PostgreSQL qua IPv4 Pooler."""
    global DB_POOL
    if not DATABASE_URL:
        logger.warning("⚠️ Chưa cấu hình biến DATABASE_URL!")
        return

    db_url = DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    try:
        DB_POOL = await asyncpg.create_pool(
            dsn=db_url,
            min_size=1,
            max_size=5,
            timeout=10,
            command_timeout=5,
            statement_cache_size=0  # Bắt buộc khi dùng Supabase Session Pooler
        )
        logger.info("✅ Đã kết nối thành công tới Supabase PostgreSQL!")
    except Exception as e:
        logger.error(f"❌ Lỗi khởi tạo kết nối Supabase: {e}")


async def db_save_note(
    platform: str,
    user_id: str,
    content: str,
    cycle: str = "hangngay",
    cycle_label: str = "Hằng ngày",
    target_date = None,
    file_id: Optional[str] = None,
    file_name: Optional[str] = None
) -> Optional[int]:
    """Lưu bài học kèm mã định danh file gốc vào Supabase."""
    if not DB_POOL:
        logger.error("Database pool chưa sẵn sàng.")
        return None
    try:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO user_notes (platform, user_id, content, cycle, cycle_label, target_date, stage, file_id, file_name)
                VALUES ($1, $2, $3, $4, $5, $6, 0, $7, $8)
                RETURNING id;
                """,
                platform, str(user_id), content, cycle, cycle_label, target_date, file_id, file_name
            )
            return row["id"] if row else None
    except Exception as e:
        logger.error(f"Lỗi khi insert note vào Supabase: {e}")
        return None


async def db_get_note_by_id(note_id: int) -> Optional[dict]:
    """Lấy chi tiết 1 bài học theo ID."""
    if not DB_POOL:
        return None
    try:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, platform, user_id, content, cycle, cycle_label, target_date, stage, file_id, file_name, created_at
                FROM user_notes
                WHERE id = $1;
                """,
                note_id
            )
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Lỗi lấy note #{note_id}: {e}")
        return None


async def db_get_user_notes(platform: str, user_id: str, limit: int = 15) -> List[dict]:
    """Lấy danh sách các bài học gần nhất của user để nạp ngữ cảnh cho AI."""
    if not DB_POOL:
        return []
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content, cycle, cycle_label, target_date, stage, file_id, file_name, created_at
                FROM user_notes
                WHERE platform = $1 AND user_id = $2
                ORDER BY id DESC
                LIMIT $3;
                """,
                platform, str(user_id), limit
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Lỗi lấy danh sách ghi chú user {user_id}: {e}")
        return []


async def db_search_notes(platform: str, user_id: str, keyword: str, limit: int = 5) -> List[dict]:
    """Tìm kiếm bài học theo từ khóa trong database."""
    if not DB_POOL:
        return []
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content, cycle_label, target_date, file_id, file_name, created_at
                FROM user_notes
                WHERE platform = $1 AND user_id = $2 AND content ILIKE $3
                ORDER BY id DESC
                LIMIT $4;
                """,
                platform, str(user_id), f"%{keyword}%", limit
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Lỗi tìm kiếm note: {e}")
        return []


async def db_update_progress(note_id: int, remembered: bool) -> str:
    """Cập nhật bậc Spaced Repetition (1-3-7-14-30 ngày) trên Supabase."""
    if not DB_POOL:
        return "⚠️ Cơ sở dữ liệu chưa sẵn sàng."

    note = await db_get_note_by_id(note_id)
    if not note:
        return f"❌ Không tìm thấy bài học #{note_id}."

    today = datetime.now(LOCAL_TZ).date()
    cycle = note.get("cycle", "hangngay")

    if cycle == "hangngay":
        return f"✅ Đã ghi nhận ôn tập bài #{note_id}. Bài này vẫn sẽ được nhắc hằng ngày."

    if remembered:
        current_stage = note.get("stage", 0)
        next_stage = current_stage + 1
        if next_stage < len(SPACED_INTERVALS):
            next_days = SPACED_INTERVALS[next_stage]
            next_date = today + timedelta(days=next_days)
            try:
                async with DB_POOL.acquire() as conn:
                    await conn.execute(
                        "UPDATE user_notes SET stage = $1, target_date = $2 WHERE id = $3;",
                        next_stage, next_date, note_id
                    )
                return f"🎉 Tuyệt vời! Bài #{note_id} đã nâng lên cấp {next_stage + 1}. Lần ôn kế tiếp sau {next_days} ngày ({next_date.strftime('%d/%m/%Y')})."
            except Exception as e:
                logger.error(f"Lỗi cập nhật tiến trình #{note_id}: {e}")
                return "❌ Lỗi lưu dữ liệu lên server."
        else:
            try:
                async with DB_POOL.acquire() as conn:
                    await conn.execute("UPDATE user_notes SET target_date = NULL WHERE id = $1;", note_id)
                return f"🏆 Xuất sắc! Bạn đã hoàn thành toàn bộ lộ trình ghi nhớ bền vững cho bài #{note_id}!"
            except Exception as e:
                logger.error(f"Lỗi cập nhật hoàn thành #{note_id}: {e}")
                return "❌ Lỗi lưu dữ liệu lên server."
    else:
        next_date = today + timedelta(days=1)
        try:
            async with DB_POOL.acquire() as conn:
                await conn.execute("UPDATE user_notes SET stage = 0, target_date = $1 WHERE id = $2;", next_date, note_id)
            return f"💪 Đừng nản lòng! Bài #{note_id} sẽ được đưa về lịch ôn ngày mai ({next_date.strftime('%d/%m/%Y')}) để củng cố trí nhớ."
        except Exception as e:
            logger.error(f"Lỗi reset tiến trình #{note_id}: {e}")
            return "❌ Lỗi lưu dữ liệu lên server."


async def db_get_due_notes(today_date) -> List[dict]:
    """Lấy danh sách các bài học đến hạn ôn tập hôm nay."""
    if not DB_POOL:
        return []
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, platform, user_id, content, cycle, cycle_label, target_date, stage, file_id, file_name
                FROM user_notes
                WHERE cycle = 'hangngay' OR target_date <= $1
                ORDER BY id DESC;
                """,
                today_date
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Lỗi lấy due notes: {e}")
        return []


# --- KẾT NỐI GROQ AI ---

async def ask_ai(prompt: str) -> str:
    """Truy vấn Groq AI qua ClientSession toàn cục."""
    if not GROQ_API_KEY:
        return "⚠️ Chưa cấu hình GROQ_API_KEY."

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    url = "https://api.groq.com/openai/v1/chat/completions"
    candidate_models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen/qwen3.8-27b"]

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
    return await ask_ai(prompt)


# --- BẢNG TRỢ GIÚP ---

HELP_MESSAGE = (
    "📖 **HỆ THỐNG LỆNH TRỢ LÝ HỌC TẬP (SUPABASE CLOUD)**\n\n"
    "📝 **1. LƯU BÀI HỌC VÀ CHU KỲ NHẮC LẶP:**\n"
    "• `/save <nội dung>`: Lưu bài theo chu kỳ chuẩn Spaced Repetition (1 - 3 - 7 - 14 - 30 ngày)\n"
    "• `/save trongngay <nội dung>`: Nhắc kiểm tra ngay hôm nay\n"
    "• `/save 3ngay <nội dung>`: Nhắc kiểm tra sau 3 ngày\n"
    "• `/save hangngay <nội dung>`: Nhắc đều đặn mỗi ngày\n\n"
    "📎 **2. ĐỌC TÀI LIỆU & TẢI LẠI FILE GỐC:**\n"
    "• Gửi file **Word (.docx)**, **Excel (.xlsx)** hoặc **PDF (.pdf)** để bot tự tóm tắt và tự động lưu mã file\n"
    "• Gửi **Ảnh chụp tài liệu/sách** để OCR và tạo câu hỏi tự kiểm tra\n"
    "• `/getfile <id>`: Yêu cầu bot gửi trả lại file gốc đính kèm của bài học\n\n"
    "🤖 **3. TRÍCH XUẤT KIẾN THỨC BẰNG AI:**\n"
    "• Bạn có thể hỏi tự nhiên bất cứ lúc nào! *(Ví dụ: \"Hôm trước tôi lưu những gì về kiểm kê bưu cục?\", \"Tóm tắt các tài liệu tôi đã gửi\")*\n"
    "• AI sẽ tự động tra cứu kho dữ liệu Supabase và giải đáp chính xác.\n\n"
    "🔍 **4. TRA CỨU & ÔN TẬP:**\n"
    "• `/list`: Xem danh sách các bài học đã lưu trên Cloud\n"
    "• `/search <từ khóa>`: Tìm kiếm bài học theo từ khóa\n"
    "• `/ontap` hoặc `/ontap <id>`: Ôn tập ngay tức thì với AI"
)


# --- XỬ LÝ LỆNH TRÊN SUPABASE ---

async def handle_save_command_core(platform: str, user_id: str, args_text: str) -> str:
    args = args_text.strip().split()
    if not args:
        return "⚠️ Cú pháp: `/save [chu_kỳ] <nội dung>`\nVí dụ: `/save 3ngay Quy trình giao nhận bưu cục`"

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

    note_id = await db_save_note(platform, user_id, content, cycle_type, cycle_label, target_date)
    if not note_id:
        return "❌ Lỗi lưu dữ liệu vào Supabase! Hãy kiểm tra lại kết nối database."

    return (
        f"✅ **Đã lưu kiến thức bền vững [Mã #{note_id}]!** ☁️\n\n"
        f"📝 *Nội dung:* {content}\n"
        f"⏰ *Chế độ:* **{cycle_label}**\n"
        f"📅 *Lần ôn tiếp theo:* `{target_date.strftime('%d/%m/%Y')}`\n\n"
        f"💡 Ôn ngay: `/ontap {note_id}`"
    )


async def handle_ontap_command_core(platform: str, user_id: str, arg_id: str) -> (str, Optional[int]):
    target_note = None
    if arg_id.strip():
        try:
            t_id = int(arg_id.strip())
            target_note = await db_get_note_by_id(t_id)
            if not target_note:
                return f"❌ Không tìm thấy bài học #{t_id} trên cơ sở dữ liệu.", None
        except ValueError:
            return "⚠️ Mã bài học phải là chữ số. Ví dụ: `/ontap 1`", None
    else:
        user_notes = await db_get_user_notes(platform, user_id, limit=1)
        if not user_notes:
            return "Bạn chưa có bài học nào. Dùng lệnh `/save <nội dung>` hoặc gửi tài liệu để lưu bài mới.", None
        target_note = user_notes[0]

    prompt = (
        f"Ghi chú kiến thức: '{target_note['content']}'.\n"
        "Hãy tóm tắt 1 câu cốt lõi ngắn, sau đó đặt 2 câu hỏi gợi mở Active Recall bằng tiếng Việt "
        "để người học tự kiểm tra xem mình đã hiểu sâu hay chưa."
    )
    ai_question = await ask_ai(prompt)
    created_str = target_note["created_at"].strftime("%d/%m/%Y") if target_note.get("created_at") else ""
    file_info = f"\n📎 *Tệp đính kèm:* `{target_note['file_name']}` (Tải lại: `/getfile {target_note['id']}`)" if target_note.get("file_id") else ""
    
    reply = (
        f"📚 **Ôn tập bài [#{target_note['id']}] (Ngày lưu: {created_str}):**{file_info}\n\n"
        f"{ai_question}"
    )
    return reply, target_note["id"]


async def handle_list_notes_core(platform: str, user_id: str) -> str:
    notes = await db_get_user_notes(platform, user_id, limit=10)
    if not notes:
        return "Danh sách học tập của bạn trên Supabase đang trống. Hãy dùng `/save` hoặc gửi tài liệu để thêm bài!"

    reply = f"📚 **Danh sách {len(notes)} bài học gần nhất đã lưu trên Cloud:**\n\n"
    for item in notes:
        n_id = item.get("id")
        t_date = item.get("target_date")
        t_date_str = t_date.strftime("%d/%m/%Y") if t_date else "Đã thuộc bền vững"
        file_tag = f" 📎 `[{item.get('file_name')}]`" if item.get("file_id") else ""
        reply += f"🔹 **[#{n_id}]**{file_tag} (Kế hoạch: `{t_date_str}`)\n👉 {item['content'][:100]}...\n\n"
    return reply


async def handle_search_notes_core(platform: str, user_id: str, keyword: str) -> str:
    kw = keyword.strip()
    if not kw:
        return "Vui lòng nhập từ khóa: `/search <từ khóa>`"

    matched = await db_search_notes(platform, user_id, kw, limit=5)
    if not matched:
        return f"🔍 Không tìm thấy kết quả nào với từ khóa '{keyword}' trên Cloud."

    reply = f"🔍 **Tìm thấy {len(matched)} kết quả phù hợp:**\n\n"
    for item in matched:
        created_str = item["created_at"].strftime("%d/%m/%Y") if item.get("created_at") else ""
        file_hint = f" (File: `/getfile {item['id']}`)" if item.get("file_id") else ""
        reply += f"📌 **[#{item['id']}]** `({created_str})`{file_hint}:\n{item['content']}\n\n"
    return reply


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
    user_name = update.effective_user.first_name or "Phong"
    welcome_text = (
        f"Chào {user_name}! 🌟✨ Chúc bạn tràn đầy năng lượng và ngập tràn cảm hứng học tập! 🚀🔥\n\n"
        f"Tôi là **Trợ lý Ôn tập & Đồng hành Tri thức (Cloud Edition)**. Hệ thống đã kết nối cơ sở dữ liệu Supabase, "
        f"hỗ trợ lưu trữ kiến thức vĩnh viễn, trích xuất thông minh và quản lý tải lại file gốc đính kèm! 📚💡"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")
    await update.message.reply_text(HELP_MESSAGE, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_MESSAGE, parse_mode="Markdown")


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args_text = " ".join(context.args) if context.args else ""
    res = await handle_save_command_core("telegram", chat_id, args_text)
    await update.message.reply_text(res, parse_mode="Markdown")


async def ontap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    arg_id = context.args[0] if context.args else ""
    res, note_id = await handle_ontap_command_core("telegram", chat_id, arg_id)
    if note_id:
        await update.message.reply_text(res, reply_markup=make_recall_keyboard(note_id))
    else:
        await update.message.reply_text(res)


async def list_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    res = await handle_list_notes_core("telegram", chat_id)
    await update.message.reply_text(res, parse_mode="Markdown")


async def search_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    kw = " ".join(context.args).strip() if context.args else ""
    res = await handle_search_notes_core("telegram", chat_id, kw)
    await update.message.reply_text(res, parse_mode="Markdown")


async def getfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gửi trả lại file gốc theo mã ID bài học."""
    if not context.args:
        await update.message.reply_text("⚠️ Vui lòng nhập mã bài học cần lấy file. Ví dụ: `/getfile 3`", parse_mode="Markdown")
        return

    try:
        note_id = int(context.args[0].strip())
    except ValueError:
        await update.message.reply_text("⚠️ Mã bài học phải là số.", parse_mode="Markdown")
        return

    note = await db_get_note_by_id(note_id)
    if not note:
        await update.message.reply_text(f"❌ Không tìm thấy bài học #{note_id}.", parse_mode="Markdown")
        return

    file_id = note.get("file_id")
    file_name = note.get("file_name", "document")
    if not file_id:
        await update.message.reply_text(f"ℹ️ Bài học #{note_id} không có tệp đính kèm gốc (được lưu dưới dạng văn bản thuần).", parse_mode="Markdown")
        return

    await update.message.reply_text(f"📤 Đang gửi lại tệp gốc `{file_name}`...")
    try:
        # Nếu là ảnh (photo)
        if file_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=f"🖼️ Tệp ảnh gốc bài #{note_id}")
        else:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=file_id, caption=f"📄 Tài liệu gốc bài #{note_id}: {file_name}")
    except Exception as e:
        logger.error(f"Lỗi gửi file Telegram: {e}")
        await update.message.reply_text(f"❌ Không thể tải lại tệp: {e}")


async def telegram_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_str = query.data
    chat_id = str(update.effective_chat.id)

    if data_str.startswith("recall_ok_"):
        n_id = int(data_str.split("_")[-1])
        msg = await db_update_progress(n_id, remembered=True)
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=int(chat_id), text=msg)

    elif data_str.startswith("recall_retry_"):
        n_id = int(data_str.split("_")[-1])
        msg = await db_update_progress(n_id, remembered=False)
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=int(chat_id), text=msg)


async def handle_chat_with_groq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý trò chuyện thông thường: Tự động trích xuất dữ liệu từ Supabase đưa vào ngữ cảnh AI."""
    user_text = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    raw_name = update.effective_user.first_name or "Phong"
    call_name = "Phong" if "phong" in raw_name.lower() or raw_name in ["Bạn", "User"] else raw_name

    greetings = ["hi", "hello", "xin chào", "chào", "chao", "alo", "start", "/start"]
    if user_text.lower() in greetings:
        await update.message.reply_text(
            f"Chào {call_name}! 🌟 Hôm nay bạn muốn tìm hiểu thêm kiến thức hay tra cứu/ôn tập bài học cũ cùng mình? Cùng bắt đầu nhé! 🚀",
            parse_mode="Markdown",
        )
        return

    await update.message.chat.send_action("typing")

    recent_notes = await db_get_user_notes("telegram", chat_id, limit=10)
    context_data = ""
    if recent_notes:
        context_data = "KHO BÀI HỌC/KIẾN THỨC ĐÃ LƯU TRÊN HỆ THỐNG CỦA NGƯỜI DÙNG:\n"
        for n in recent_notes:
            c_date = n["created_at"].strftime("%d/%m/%Y") if n.get("created_at") else "Gần đây"
            f_note = f" (Có tệp gốc: {n['file_name']})" if n.get("file_id") else ""
            context_data += f"- [Mã #{n['id']}] (Ngày {c_date}){f_note}: {n['content']}\n"
    else:
        context_data = "Người dùng hiện chưa lưu bài học nào trong cơ sở dữ liệu.\n"

    prompt = (
        f"Bạn là người bạn trợ lý học tập thân thiết của anh {call_name}.\n"
        f"Dưới đây là dữ liệu các bài học đã lưu của {call_name}:\n"
        f"--- BẮT ĐẦU DỮ LIỆU ĐÃ LƯU ---\n"
        f"{context_data}"
        f"--- KẾT THÚC DỮ LIỆU ĐÃ LƯU ---\n\n"
        f"YÊU CẦU ĐỐI THOẠI & TRÍCH XUẤT:\n"
        f"1. Nếu {call_name} hỏi, tìm kiếm, tóm tắt hoặc nhắc lại những bài học/quy trình đã từng lưu: hãy trích xuất chính xác từ dữ liệu trên, nêu rõ mã bài học (#ID) và giải đáp chi tiết, mạch lạc.\n"
        f"2. Nếu bài học có tệp gốc, hãy nhắc {call_name} có thể gõ /getfile <id> để tải lại tệp bất kỳ lúc nào.\n"
        f"3. Xưng hô gần gũi, gọi tên '{call_name}', dùng emoji tích cực (🌟, 🚀, 💡).\n"
        f"4. Trả lời bằng tiếng Việt tự nhiên, súc tích.\n\n"
        f"Tin nhắn của {call_name}: '{user_text}'"
    )

    reply = await ask_ai(prompt)
    await update.message.reply_text(reply)


async def telegram_handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    file_name = doc.file_name or "document"
    chat_id = str(update.effective_chat.id)
    ext = file_name.split(".")[-1].lower() if "." in file_name else ""

    status_msg = await update.message.reply_text(f"📥 Đang tải và phân tích tài liệu `{file_name}`...")
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
            # Tự động lưu ảnh vào Supabase
            note_id = await db_save_note(
                platform="telegram",
                user_id=chat_id,
                content=f"Hình ảnh: {file_name}\n{analysis}",
                cycle="hangngay",
                cycle_label="Hình ảnh tài liệu",
                target_date=datetime.now(LOCAL_TZ).date(),
                file_id=doc.file_id,
                file_name=file_name
            )
            await status_msg.edit_text(
                f"🖼️ **PHÂN TÍCH HÌNH ẢNH [Mã #{note_id}]:**\n\n{analysis}\n\n"
                f"*(💾 Đã tự động lưu vào Cloud. Tải lại ảnh: `/getfile {note_id}`)*"
            )
            return
        else:
            await status_msg.edit_text(f"⚠️ Chưa hỗ trợ định dạng `.{ext}`.")
            return

        analysis = await process_document_content(file_name, txt)
        # Tự động lưu tài liệu vào Supabase
        note_id = await db_save_note(
            platform="telegram",
            user_id=chat_id,
            content=f"Tài liệu {file_name}:\n{analysis}",
            cycle="hangngay",
            cycle_label="Tài liệu văn phòng",
            target_date=datetime.now(LOCAL_TZ).date(),
            file_id=doc.file_id,
            file_name=file_name
        )
        await status_msg.edit_text(
            f"📄 **KẾT QUẢ ĐỌC TÀI LIỆU [Mã #{note_id}]:** `{file_name}`\n\n"
            f"{analysis}\n\n"
            f"*(💾 Đã lưu vào Supabase. Tải lại tệp gốc: `/getfile {note_id}`)*"
        )
    except Exception as e:
        logger.error(f"Lỗi đọc file Telegram: {e}")
        await status_msg.edit_text(f"❌ Lỗi xử lý file `{file_name}`: {e}")


async def telegram_handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("📸 Đang nhận diện tài liệu qua Groq Vision AI...")
    chat_id = str(update.effective_chat.id)
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
        # Tự động lưu ảnh vào Supabase
        note_id = await db_save_note(
            platform="telegram",
            user_id=chat_id,
            content=f"Ảnh chụp ghi chú:\n{analysis}",
            cycle="hangngay",
            cycle_label="Ảnh chụp tài liệu",
            target_date=datetime.now(LOCAL_TZ).date(),
            file_id=photo.file_id,
            file_name="photo.jpg"
        )
        await status_msg.edit_text(
            f"🖼️ **KẾT QUẢ ĐỌC HÌNH ẢNH [Mã #{note_id}]:**\n\n{analysis}\n\n"
            f"*(💾 Đã lưu vào Supabase. Tải lại ảnh: `/getfile {note_id}`)*"
        )
    except Exception as e:
        logger.error(f"Lỗi ảnh Telegram: {e}")
        await status_msg.edit_text(f"❌ Lỗi phân tích ảnh: {e}")


# --- KẾT NỐI VÀ XỬ LÝ LARK SUITE ---

async def get_lark_tenant_token() -> str:
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
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
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

    if body.get("type") == "url_verification":
        return web.json_response({"challenge": body.get("challenge", "")})

    header = body.get("header", {})
    if header.get("event_type") == "im.message.receive_v1":
        event = body.get("event", {})
        open_id = event.get("sender", {}).get("sender_id", {}).get("open_id")
        message = event.get("message", {})
        if open_id:
            asyncio.create_task(dispatch_lark_event(open_id, message))

    if body.get("action"):
        action_data = body.get("action", {}).get("value", {})
        act_type = action_data.get("action")
        note_id = action_data.get("note_id")

        if act_type and note_id:
            remembered = (act_type == "recall_ok")
            msg = await db_update_progress(int(note_id), remembered=remembered)
            return web.json_response({"toast": {"type": "success", "content": msg}})

    return web.json_response({"status": "success"})


async def dispatch_lark_event(open_id: str, message: dict):
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
        # Lưu vào Supabase kèm file_key
        note_id = await db_save_note(
            platform="lark",
            user_id=open_id,
            content=f"Tài liệu {file_name}:\n{analysis}",
            cycle="hangngay",
            cycle_label="Tài liệu văn phòng",
            target_date=datetime.now(LOCAL_TZ).date(),
            file_id=file_key,
            file_name=file_name
        )
        await send_lark_message(open_id, f"📄 **KẾT QUẢ ĐỌC TÀI LIỆU [Mã #{note_id}]:** `{file_name}`\n\n{analysis}")
        return

    if msg_type == "image":
        image_key = content_json.get("image_key")
        await send_lark_message(open_id, "📸 Đang phân tích chữ trong ảnh qua Groq Vision AI...")
        img_bytes = await download_lark_file(msg_id, image_key, "image")
        analysis = await ask_ai_vision(img_bytes, "Đọc chữ OCR và đặt 2 câu hỏi Active Recall.")
        note_id = await db_save_note(
            platform="lark",
            user_id=open_id,
            content=f"Ảnh tài liệu:\n{analysis}",
            cycle="hangngay",
            cycle_label="Ảnh chụp tài liệu",
            target_date=datetime.now(LOCAL_TZ).date(),
            file_id=image_key,
            file_name="image.jpg"
        )
        await send_lark_message(open_id, f"🖼️ **KẾT QUẢ ĐỌC HÌNH ẢNH [Mã #{note_id}]:**\n\n{analysis}")
        return

    raw = content_json.get("text", "").strip()
    if not raw:
        return

    if raw.lower() in ["/start", "hi", "hello", "chào", "chao"]:
        reply = "Chào Phong! 🌟 Chúc bạn một ngày tràn đầy năng lượng và cảm hứng học tập! 🚀💡\n\n" + HELP_MESSAGE
        await send_lark_message(open_id, reply)
    elif raw.lower() in ["/help", "/menu", "/lenh"]:
        await send_lark_message(open_id, HELP_MESSAGE)
    elif raw.startswith("/save"):
        reply = await handle_save_command_core("lark", open_id, raw[5:].strip())
        await send_lark_message(open_id, reply)
    elif raw == "/list":
        reply = await handle_list_notes_core("lark", open_id)
        await send_lark_message(open_id, reply)
    elif raw.startswith("/search"):
        reply = await handle_search_notes_core("lark", open_id, raw[7:].strip())
        await send_lark_message(open_id, reply)
    elif raw.startswith("/ontap"):
        reply, note_id = await handle_ontap_command_core("lark", open_id, raw[6:].strip())
        if note_id:
            await send_lark_interactive_card(open_id, f"📚 Ôn tập bài #{note_id}", reply, note_id)
        else:
            await send_lark_message(open_id, reply)
    else:
        recent_notes = await db_get_user_notes("lark", open_id, limit=10)
        context_data = ""
        if recent_notes:
            context_data = "KHO BÀI HỌC/KIẾN THỨC ĐÃ LƯU TRÊN HỆ THỐNG:\n"
            for n in recent_notes:
                c_date = n["created_at"].strftime("%d/%m/%Y") if n.get("created_at") else "Gần đây"
                context_data += f"- [Mã #{n['id']}] (Ngày {c_date}): {n['content']}\n"
        else:
            context_data = "Chưa có bài học nào được lưu.\n"

        prompt = (
            f"Bạn là người bạn trợ lý học tập thân thiết của anh Phong.\n"
            f"Dưới đây là các bài học Phong đã lưu:\n{context_data}\n\n"
            f"Hãy giải đáp tin nhắn của Phong, nếu liên quan bài học cũ hãy trích xuất chính xác kèm mã bài học (#ID). "
            f"Xưng hô tự nhiên, gọi tên Phong, dùng emoji vui vẻ (🌟, 🚀, 💡): '{raw}'"
        )
        reply = await ask_ai(prompt)
        await send_lark_message(open_id, reply)


# --- BỘ LẬP LỊCH TỰ ĐỘNG ---

async def dynamic_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(LOCAL_TZ)
    current_time_str = now.strftime("%H:%M")
    today_date = now.date()

    is_morning = (current_time_str == "10:00")
    is_evening = (current_time_str == "19:30")
    is_night = (current_time_str == "22:00")

    if not (is_morning or is_evening or is_night):
        return

    if is_night:
        msg = (
            "🌙 **Đến giờ tổng kết kiến thức ngày hôm nay!** 🌟✨\n\n"
            "Hôm nay bạn học được bài học mới nào? Hãy gửi ngay cú pháp:\n"
            "`/save [chu_kỳ] <nội dung>` hoặc gửi file tài liệu để lưu vào Supabase Cloud nhé!"
        )
        try:
            if DB_POOL:
                async with DB_POOL.acquire() as conn:
                    users = await conn.fetch("SELECT DISTINCT platform, user_id FROM user_notes;")
                    for u in users:
                        p, uid = u["platform"], u["user_id"]
                        if p == "telegram":
                            await context.bot.send_message(chat_id=int(uid), text=msg, parse_mode="Markdown")
                        else:
                            await send_lark_message(uid, msg)
        except Exception as e:
            logger.error(f"Lỗi gửi nhắc đêm: {e}")
        return

    due_notes = await db_get_due_notes(today_date)
    if not due_notes:
        return

    user_map = {}
    for n in due_notes:
        key = (n["platform"], n["user_id"])
        if key not in user_map:
            user_map[key] = n

    for (platform, user_id), selected_note in user_map.items():
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

        n_id = selected_note["id"]
        try:
            if platform == "telegram":
                await context.bot.send_message(
                    chat_id=int(user_id),
                    text=body,
                    reply_markup=make_recall_keyboard(n_id),
                    parse_mode="Markdown",
                )
            else:
                await send_lark_interactive_card(user_id, title, body, n_id)
        except Exception as e:
            logger.error(f"Lỗi gửi tin nhắc định kỳ ({platform}): {e}")


# --- WEB SERVER & APP LIFECYCLE ---

async def handle_ping(request):
    return web.Response(text="Trợ lý Học tập Thông minh (Supabase Cloud + Telegram + Lark) đang hoạt động 24/7!")


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
        BotCommand("save", "Lưu bài học theo chu kỳ Spaced Repetition"),
        BotCommand("ontap", "Ôn tập kiến thức tức thì qua AI"),
        BotCommand("list", "Xem các bài học đã ghi nhận"),
        BotCommand("search", "Tìm bài học cũ theo từ khóa"),
        BotCommand("getfile", "Tải lại tệp gốc đính kèm (/getfile <id>)"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except Exception as e:
        logger.error(f"Lỗi cài đặt Telegram Bot commands: {e}")


async def run_bot():
    global GLOBAL_SESSION
    GLOBAL_SESSION = ClientSession()

    await init_db()

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
    application.add_handler(CommandHandler("getfile", getfile_command))
    application.add_handler(CallbackQueryHandler(telegram_callback_handler))

    # Đăng ký nhận file và hình ảnh
    application.add_handler(MessageHandler(filters.PHOTO, telegram_handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, telegram_handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_with_groq))

    # Job Queue nhắc nhở mỗi 60 giây
    if application.job_queue:
        application.job_queue.run_repeating(dynamic_scheduler_job, interval=60, first=10)

    # Khởi chạy Web Server cho Lark
    await run_web_server()

    try:
        async with application:
            await application.start()
            await application.updater.start_polling()
            logger.info("🚀 Trợ lý Học tập Supabase Cloud đã sẵn sàng hoạt động!")
            while True:
                await asyncio.sleep(3600)
    finally:
        if GLOBAL_SESSION and not GLOBAL_SESSION.closed:
            await GLOBAL_SESSION.close()
        if DB_POOL:
            await DB_POOL.close()


def main():
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot đã dừng hoạt động an toàn.")


if __name__ == "__main__":
    main()
