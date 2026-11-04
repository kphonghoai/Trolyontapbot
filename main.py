"""
Trợ lý Ôn tập & Nhắc nhở Kiến thức Thông minh (Groq AI) - Bản Push Notification Ưu Tiên Cao (Phương Án A)
- Lưu trữ vĩnh viễn trên Supabase PostgreSQL (IPv4 Pooler)
- Tích hợp Push Notification ưu tiên cao: Buộc rung & phát chuông, tự động Ghim tin nhắn (Pin) trên Telegram
- Lark Suite kích hoạt Thẻ cảnh báo màu đỏ (Red Template) kèm rung chuông nổi bật
- Hỗ trợ nhắc nhở theo khung giờ / ngày giờ tùy chọn (/nhac <id> <giờ> hoặc /save HH:MM ...)
- Hỗ trợ gắn nhãn phân loại (#quytrinh, #vandon, #cod...) và lọc theo thẻ (/list #nhan)
- Hỗ trợ Active Recall đối thoại: AI chấm điểm câu trả lời văn bản & voice trực tiếp
- Đồng bộ chung dữ liệu thời gian thực giữa Telegram và Lark Suite
- Tự động lưu file_id của tài liệu/ảnh và hỗ trợ lệnh /getfile để tải lại file gốc
- Hỗ trợ xóa chọn lọc từng bài học (/xoa <id>) và xóa toàn bộ (/xoatatca xacnhan)
- Chạy đồng thời Telegram Bot & Lark Suite Webhook
"""

import json
import logging
import os
import io
import re
import base64
import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

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

# Lưu trạng thái người dùng đang chờ trả lời câu hỏi Active Recall {user_id: note_id}
ACTIVE_RECALL_SESSIONS: Dict[str, int] = {}


# --- HỖ TRỢ XỬ LÝ THỜI GIAN HẸN GIỜ ---

def parse_custom_datetime(time_str: str) -> Optional[datetime]:
    """
    Phân tích chuỗi ngày giờ linh hoạt theo múi giờ Việt Nam (UTC+7):
    - HH:MM (ví dụ 14:30, 08:00) -> Hôm nay hoặc ngày mai nếu giờ đã qua
    - DD/MM HH:MM (ví dụ 05/11 15:30)
    - DD/MM/YYYY HH:MM
    """
    now = datetime.now(LOCAL_TZ)
    time_str = time_str.strip()

    # Định dạng HH:MM
    match_hm = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if match_hm:
        h, m = int(match_hm.group(1)), int(match_hm.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            target_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target_dt <= now:
                target_dt += timedelta(days=1)
            return target_dt

    # Định dạng DD/MM HH:MM
    match_dhm = re.match(r"^(\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2})$", time_str)
    if match_dhm:
        d, mon, h, m = int(match_dhm.group(1)), int(match_dhm.group(2)), int(match_dhm.group(3)), int(match_dhm.group(4))
        try:
            return datetime(now.year, mon, d, h, m, 0, tzinfo=LOCAL_TZ)
        except ValueError:
            return None

    # Định dạng DD/MM/YYYY HH:MM
    match_full = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s+(\d{1,2}):(\d{2})$", time_str)
    if match_full:
        d, mon, y, h, m = int(match_full.group(1)), int(match_full.group(2)), int(match_full.group(3)), int(match_full.group(4)), int(match_full.group(5))
        try:
            return datetime(y, mon, d, h, m, 0, tzinfo=LOCAL_TZ)
        except ValueError:
            return None

    return None


def extract_hashtags(text: str) -> List[str]:
    tags = re.findall(r"#\w+", text)
    return [t.lower() for t in set(tags)]


# --- KẾT NỐI VÀ QUẢN LÝ DỮ LIỆU SUPABASE POSTGRESQL ---

async def init_db():
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
            statement_cache_size=0
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
    file_name: Optional[str] = None,
    tags: Optional[List[str]] = None,
    remind_at: Optional[datetime] = None
) -> Optional[int]:
    if not DB_POOL:
        logger.error("Database pool chưa sẵn sàng.")
        return None
    try:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO user_notes (platform, user_id, content, cycle, cycle_label, target_date, stage, file_id, file_name, tags, remind_at)
                VALUES ($1, $2, $3, $4, $5, $6, 0, $7, $8, $9, $10)
                RETURNING id;
                """,
                platform, str(user_id), content, cycle, cycle_label, target_date, file_id, file_name, tags or [], remind_at
            )
            return row["id"] if row else None
    except Exception as e:
        logger.error(f"Lỗi khi insert note vào Supabase: {e}")
        return None


async def db_get_note_by_id(note_id: int) -> Optional[dict]:
    if not DB_POOL:
        return None
    try:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, platform, user_id, content, cycle, cycle_label, target_date, stage, file_id, file_name, tags, remind_at, created_at
                FROM user_notes
                WHERE id = $1;
                """,
                note_id
            )
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Lỗi lấy note #{note_id}: {e}")
        return None


async def db_update_remind_time(note_id: int, remind_at: Optional[datetime]) -> bool:
    """Cập nhật hoặc xóa mốc hẹn giờ tùy chọn của bài học."""
    if not DB_POOL:
        return False
    try:
        async with DB_POOL.acquire() as conn:
            await conn.execute("UPDATE user_notes SET remind_at = $1 WHERE id = $2;", remind_at, note_id)
            return True
    except Exception as e:
        logger.error(f"Lỗi cập nhật lịch hẹn #{note_id}: {e}")
        return False


async def db_get_user_notes(platform: str, user_id: str, limit: int = 15, tag_filter: Optional[str] = None) -> List[dict]:
    if not DB_POOL:
        return []
    try:
        async with DB_POOL.acquire() as conn:
            if tag_filter:
                clean_tag = tag_filter.lower()
                rows = await conn.fetch(
                    """
                    SELECT id, platform, content, cycle, cycle_label, target_date, stage, file_id, file_name, tags, remind_at, created_at
                    FROM user_notes
                    WHERE $1 = ANY(tags) OR content ILIKE $2
                    ORDER BY id DESC
                    LIMIT $3;
                    """,
                    clean_tag, f"%{clean_tag}%", limit
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, platform, content, cycle, cycle_label, target_date, stage, file_id, file_name, tags, remind_at, created_at
                    FROM user_notes
                    ORDER BY id DESC
                    LIMIT $1;
                    """,
                    limit
                )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Lỗi lấy danh sách ghi chú: {e}")
        return []


async def db_search_notes(platform: str, user_id: str, keyword: str, limit: int = 5) -> List[dict]:
    if not DB_POOL:
        return []
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content, cycle_label, target_date, file_id, file_name, tags, remind_at, created_at
                FROM user_notes
                WHERE content ILIKE $1
                ORDER BY id DESC
                LIMIT $2;
                """,
                f"%{keyword}%", limit
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Lỗi tìm kiếm note: {e}")
        return []


async def db_update_progress(note_id: int, remembered: bool) -> str:
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
    if not DB_POOL:
        return []
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, platform, user_id, content, cycle, cycle_label, target_date, stage, file_id, file_name, tags, remind_at
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


async def db_get_custom_remind_notes(now_dt: datetime) -> List[dict]:
    """Lấy các bài học có lịch hẹn giờ tùy chọn đã đến hạn nhắc."""
    if not DB_POOL:
        return []
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, platform, user_id, content, cycle, cycle_label, target_date, stage, file_id, file_name, tags, remind_at
                FROM user_notes
                WHERE remind_at IS NOT NULL AND remind_at <= $1
                ORDER BY remind_at ASC;
                """,
                now_dt
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Lỗi lấy custom remind notes: {e}")
        return []


async def db_delete_single_note(platform: str, user_id: str, note_id: int) -> bool:
    if not DB_POOL:
        return False
    try:
        async with DB_POOL.acquire() as conn:
            result = await conn.execute("DELETE FROM user_notes WHERE id = $1;", note_id)
            return result.endswith("1")
    except Exception as e:
        logger.error(f"Lỗi xóa bài #{note_id}: {e}")
        return False


async def db_delete_user_data(platform: str, user_id: str) -> bool:
    if not DB_POOL:
        return False
    try:
        async with DB_POOL.acquire() as conn:
            await conn.execute("TRUNCATE TABLE user_notes RESTART IDENTITY;")
            return True
    except Exception as e:
        logger.error(f"Lỗi làm sạch dữ liệu: {e}")
        return False


# --- KẾT NỐI GROQ AI ---

async def ask_ai(prompt: str) -> str:
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

    candidate_vision_models = [
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "qwen/qwen3.8-27b",
        "meta-llama/llama-4-maverick-17b-128e-instruct",
    ]

    last_error = ""
    for v_model in candidate_vision_models:
        payload = {
            "model": v_model,
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
                else:
                    err_msg = data.get("error", {}).get("message", str(data))
                    last_error = f"{v_model}: {err_msg}"
        except Exception as e:
            last_error = f"{v_model} exception: {e}"

    return f"Lỗi phân tích ảnh từ các model Vision: {last_error}"


async def ask_ai_transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    if not GROQ_API_KEY:
        return ""
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    
    data = web.FormData()
    data.add_field("file", audio_bytes, filename=filename, content_type="audio/ogg")
    data.add_field("model", "whisper-large-v3-turbo")
    data.add_field("language", "vi")

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        async with GLOBAL_SESSION.post(url, data=data, headers=headers, timeout=30) as resp:
            if resp.status == 200:
                result = await resp.json()
                return result.get("text", "").strip()
            else:
                logger.error(f"Lỗi Whisper ({resp.status}): {await resp.text()}")
    except Exception as e:
        logger.error(f"Lỗi kết nối Whisper Groq: {e}")
    return ""


async def evaluate_active_recall_answer(note_content: str, user_answer: str) -> (str, int):
    prompt = (
        f"Bạn là người kiểm tra kiến thức công tâm và tận tâm.\n\n"
        f"--- NỘI DUNG BÀI HỌC GỐC ---\n{note_content}\n"
        f"--- CÂU TRẢ LỜI CỦA NGƯỜI HỌC ---\n{user_answer}\n\n"
        f"YÊU CẦU ĐÁNH GIÁ:\n"
        f"1. So sánh chi tiết câu trả lời với nội dung gốc.\n"
        f"2. Nêu rõ: Ý đúng/chuẩn xác đã nắm được; Ý còn thiếu hoặc hiểu chưa sát.\n"
        f"3. Cho điểm từ 1 đến 10 theo mức độ hiểu sâu.\n"
        f"4. Trả về đúng định dạng sau ở cuối phản hồi: [DIEM: X] (với X là số nguyên từ 1 đến 10).\n"
        f"Trình bày chuyên nghiệp, động viên, dùng tiếng Việt ngắn gọn, súc tích."
    )
    feedback = await ask_ai(prompt)
    score_match = re.search(r"\[DIEM:\s*(\d+)\]", feedback)
    score = int(score_match.group(1)) if score_match else 7
    clean_feedback = re.sub(r"\[DIEM:\s*\d+\]", "", feedback).strip()
    return clean_feedback, score


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
    "🚨 **1. PUSH NOTIFICATION & HẸN GIỜ (MỨC ĐỘ ƯU TIÊN CAO):**\n"
    "• `/save 14:30 #vandon Kiểm tra gói hàng`: Đặt nhắc chính xác lúc 14:30 hôm nay\n"
    "• `/save 05/11 09:00 #quytrinh Họp bưu cục`: Đặt nhắc ngày và giờ cụ thể\n"
    "• `/nhac <id> <thời gian>`: Đặt giờ nhắc cho bài đã có (Ví dụ: `/nhac 8 16:00` hoặc `/nhac 8 05/11 08:30`)\n"
    "*(⚡ Khi đến giờ, bot sẽ tự động rung chuông và Ghim tin nhắn để đánh thức màn hình khóa)*\n\n"
    "📝 **2. LƯU BÀI HỌC VÀ GẮN NHÃN (TAGS):**\n"
    "• `/save #quytrinh #vandon <nội dung>`: Lưu bài theo lộ trình Spaced Repetition\n"
    "• `/save 3ngay #cod <nội dung>`: Lưu bài ôn sau 3 ngày\n"
    "• `/save hangngay <nội dung>`: Nhắc đều đặn mỗi ngày\n\n"
    "🧠 **3. ÔN TẬP & AI CHẤM ĐIỂM RECALL:**\n"
    "• `/ontap` hoặc `/ontap <id>`: Tạo câu hỏi ôn tập tức thì\n"
    "• Bấm nút **'✍️ Trả lời câu hỏi'** rồi gửi văn bản hoặc tin nhắn thoại (voice) để AI chấm điểm\n\n"
    "📎 **4. ĐỌC TÀI LIỆU & TẢI LẠI FILE GỐC:**\n"
    "• Gửi file **Word (.docx)**, **Excel (.xlsx)** hoặc **PDF (.pdf)** để bot tự tóm tắt\n"
    "• Gửi **Ảnh chụp tài liệu** để OCR\n"
    "• `/getfile <id>`: Tải lại file gốc đính kèm\n\n"
    "🔍 **5. TRA CỨU THEO CHUYÊN MỤC:**\n"
    "• `/list`: Xem tất cả bài học\n"
    "• `/list #quytrinh`: Lọc riêng bài học theo hashtag\n"
    "• `/search <từ khóa>`: Tìm kiếm theo từ khóa\n\n"
    "🗑️ **6. QUẢN LÝ DỮ LIỆU:**\n"
    "• `/xoa <id>`: Xóa chọn lọc 1 bài học theo mã ID\n"
    "• `/xoatatca`: Xóa sạch toàn bộ bộ nhớ có xác nhận an toàn"
)


# --- XỬ LÝ LỆNH TRÊN SUPABASE ---

async def handle_save_command_core(platform: str, user_id: str, args_text: str) -> str:
    args = args_text.strip().split()
    if not args:
        return (
            "⚠️ Cú pháp:\n"
            "• Hẹn giờ: `/save 14:30 [#nhãn] <nội dung>` hoặc `/save 05/11 08:30 <nội dung>`\n"
            "• Chu kỳ: `/save [chu_kỳ] [#nhãn] <nội dung>`\n"
            "Ví dụ: `/save 15:30 #vandon Đối soát tiền COD bưu cục`"
        )

    now = datetime.now(LOCAL_TZ)
    today_date = now.date()

    remind_at = None
    cycle_type = "spaced"
    cycle_label = "Lộ trình ghi nhớ bền vững (1-3-7-14-30 ngày)"
    target_date = today_date + timedelta(days=SPACED_INTERVALS[0])
    raw_content = ""

    # 1. Thử nhận diện hẹn ngày giờ: DD/MM HH:MM (2 tham số đầu)
    if len(args) >= 3 and re.match(r"^\d{1,2}[/-]\d{1,2}$", args[0]) and re.match(r"^\d{1,2}:\d{2}$", args[1]):
        parsed_dt = parse_custom_datetime(f"{args[0]} {args[1]}")
        if parsed_dt:
            remind_at = parsed_dt
            cycle_type = "custom_time"
            cycle_label = f"Hẹn giờ: {remind_at.strftime('%H:%M ngày %d/%m/%Y')}"
            target_date = remind_at.date()
            raw_content = " ".join(args[2:]).strip()

    # 2. Thử nhận diện hẹn giờ trong ngày: HH:MM (tham số đầu)
    if not remind_at and re.match(r"^\d{1,2}:\d{2}$", args[0]):
        parsed_dt = parse_custom_datetime(args[0])
        if parsed_dt:
            remind_at = parsed_dt
            cycle_type = "custom_time"
            cycle_label = f"Hẹn giờ: {remind_at.strftime('%H:%M ngày %d/%m/%Y')}"
            target_date = remind_at.date()
            raw_content = " ".join(args[1:]).strip()

    # 3. Nếu không phải hẹn giờ, kiểm tra chu kỳ truyền thống
    if not remind_at:
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
            raw_content = " ".join(args[1:]).strip()
            if cycle_type == "spaced":
                target_date = today_date + timedelta(days=SPACED_INTERVALS[0])
            elif cycle_type == "custom":
                target_date = today_date + timedelta(days=offset_days)
            else:
                target_date = today_date
        else:
            raw_content = " ".join(args).strip()

    if not raw_content:
        return "⚠️ Bạn chưa nhập nội dung kiến thức cần ghi nhớ!"

    tags = extract_hashtags(raw_content)

    note_id = await db_save_note(
        platform, user_id, raw_content, cycle_type, cycle_label, target_date,
        tags=tags, remind_at=remind_at
    )
    if not note_id:
        return "❌ Lỗi lưu dữ liệu vào Supabase! Hãy kiểm tra lại kết nối database."

    tags_display = f"\n🏷️ *Nhãn chuyên mục:* `{' '.join(tags)}`" if tags else ""
    remind_display = f"\n⏰ *Thời gian hẹn nhắc:* `{remind_at.strftime('%H:%M ngày %d/%m/%Y')}` *(Bật Push Notification)*" if remind_at else f"\n📅 *Lần ôn tiếp theo:* `{target_date.strftime('%d/%m/%Y')}`"

    return (
        f"✅ **Đã lưu kiến thức [Mã #{note_id}]!** ☁️\n\n"
        f"📝 *Nội dung:* {raw_content}\n"
        f"⚙️ *Chế độ:* **{cycle_label}**"
        f"{tags_display}"
        f"{remind_display}\n\n"
        f"💡 Ôn ngay: `/ontap {note_id}` | Hẹn giờ lại: `/nhac {note_id} <giờ>`"
    )


async def handle_nhac_command_core(platform: str, user_id: str, args_text: str) -> str:
    """Xử lý lệnh đặt hẹn giờ cho bài học cũ: /nhac <id> <thời gian>"""
    args = args_text.strip().split()
    if len(args) < 2:
        return (
            "⚠️ Cú pháp đặt lịch hẹn nhắc bài học:\n"
            "• `/nhac <id> <HH:MM>` (Ví dụ: `/nhac 8 15:30` - nhắc lúc 15:30 hôm nay)\n"
            "• `/nhac <id> <DD/MM HH:MM>` (Ví dụ: `/nhac 8 05/11 09:00`)"
        )

    try:
        note_id = int(args[0])
    except ValueError:
        return "⚠️ Mã bài học phải là số. Ví dụ: `/nhac 8 15:30`"

    note = await db_get_note_by_id(note_id)
    if not note:
        return f"❌ Không tìm thấy bài học #{note_id}."

    time_input = " ".join(args[1:])
    parsed_dt = parse_custom_datetime(time_input)
    if not parsed_dt:
        return f"⚠️ Định dạng thời gian '{time_input}' không hợp lệ. Vui lòng nhập dạng `15:30` hoặc `05/11 09:00`."

    success = await db_update_remind_time(note_id, parsed_dt)
    if success:
        return (
            f"⏰ **Đã lên lịch hẹn nhắc bài [#{note_id}] thành công!**\n\n"
            f"📖 *Nội dung:* {note['content'][:120]}...\n"
            f"🔔 *Thời điểm nhắc:* `{parsed_dt.strftime('%H:%M ngày %d/%m/%Y')}`\n"
            f"🚨 *Chế độ:* **Push Notification Ưu Tiên Cao (Tự động Ghim tin nhắn & Rung chuông)** 🎯"
        )
    return "❌ Lỗi khi cập nhật lịch hẹn lên cơ sở dữ liệu."


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
    tags_str = f"\n🏷️ *Nhãn:* `{' '.join(target_note.get('tags', []))}`" if target_note.get("tags") else ""
    remind_str = f"\n⏰ *Lịch hẹn nhắc:* `{target_note['remind_at'].astimezone(LOCAL_TZ).strftime('%H:%M %d/%m')}`" if target_note.get("remind_at") else ""

    reply = (
        f"📚 **Ôn tập bài [#{target_note['id']}] (Ngày lưu: {created_str}):**{tags_str}{remind_str}{file_info}\n\n"
        f"{ai_question}"
    )
    return reply, target_note["id"]


async def handle_list_notes_core(platform: str, user_id: str, tag_filter: Optional[str] = None) -> str:
    notes = await db_get_user_notes(platform, user_id, limit=10, tag_filter=tag_filter)
    header_extra = f" thuộc nhãn `{tag_filter}`" if tag_filter else ""
    if not notes:
        return f"Danh sách học tập của bạn trên Supabase{header_extra} đang trống!"

    reply = f"📚 **Danh sách {len(notes)} bài học gần nhất{header_extra}:**\n\n"
    for item in notes:
        n_id = item.get("id")
        t_date = item.get("target_date")
        t_date_str = t_date.strftime("%d/%m/%Y") if t_date else "Đã thuộc bền vững"
        file_tag = f" 📎 `[{item.get('file_name')}]`" if item.get("file_id") else ""
        tags_tag = f" 🏷️`{' '.join(item.get('tags', []))}`" if item.get("tags") else ""
        remind_tag = f" ⏰`{item['remind_at'].astimezone(LOCAL_TZ).strftime('%H:%M %d/%m')}`" if item.get("remind_at") else ""
        reply += f"🔹 **[#{n_id}]**{tags_tag}{remind_tag}{file_tag} (Kế hoạch: `{t_date_str}`)\n👉 {item['content'][:100]}...\n\n"
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
        tags_str = f" 🏷️`{' '.join(item.get('tags', []))}`" if item.get("tags") else ""
        remind_str = f" ⏰`{item['remind_at'].astimezone(LOCAL_TZ).strftime('%H:%M %d/%m')}`" if item.get("remind_at") else ""
        reply += f"📌 **[#{item['id']}]** `({created_str})`{tags_str}{remind_str}{file_hint}:\n{item['content']}\n\n"
    return reply


# --- GIAO DIỆN NÚT BẤM (TELEGRAM INLINE KEYBOARD) ---

def make_recall_keyboard(note_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("✍️ Trả lời câu hỏi (AI Chấm)", callback_data=f"recall_answer_{note_id}"),
        ],
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
        f"Tôi là **Trợ lý Ôn tập & Đồng hành Tri thức (Cloud Edition)**. Hệ thống đã nâng cấp:\n"
        f"🚨 **Push Notification Ưu Tiên Cao**: Hẹn giờ nhắc tự động bật chuông & Ghim tin nhắn\n"
        f"⏰ **Hẹn giờ tùy chọn**: `/save 14:30 <nội dung>` hoặc `/nhac <id> 15:00`\n"
        f"🏷️ **Hashtag phân loại**: Lưu kèm `#quytrinh`, `#vandon` và lọc qua `/list #nhãn`\n"
        f"🧠 **AI Chấm Điểm Recall**: Gửi văn bản hoặc tin nhắn thoại (voice) để AI đánh giá mức độ hiểu sâu!"
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


async def nhac_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args_text = " ".join(context.args) if context.args else ""
    res = await handle_nhac_command_core("telegram", chat_id, args_text)
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
    tag_filter = context.args[0] if context.args else None
    res = await handle_list_notes_core("telegram", chat_id, tag_filter)
    await update.message.reply_text(res, parse_mode="Markdown")


async def search_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    kw = " ".join(context.args).strip() if context.args else ""
    res = await handle_search_notes_core("telegram", chat_id, kw)
    await update.message.reply_text(res, parse_mode="Markdown")


async def getfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await update.message.reply_text(f"ℹ️ Bài học #{note_id} không có tệp đính kèm gốc.", parse_mode="Markdown")
        return

    await update.message.reply_text(f"📤 Đang gửi lại tệp gốc `{file_name}`...")
    try:
        if file_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=f"🖼️ Tệp ảnh gốc bài #{note_id}")
        else:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=file_id, caption=f"📄 Tài liệu gốc bài #{note_id}: {file_name}")
    except Exception as e:
        logger.error(f"Lỗi gửi file Telegram: {e}")
        await update.message.reply_text(f"❌ Không thể tải lại tệp: {e}")


async def xoa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("⚠️ Vui lòng nhập mã bài học cần xóa. Ví dụ: `/xoa 3`", parse_mode="Markdown")
        return

    try:
        note_id = int(context.args[0].strip())
    except ValueError:
        await update.message.reply_text("⚠️ Mã bài học phải là số. Ví dụ: `/xoa 3`", parse_mode="Markdown")
        return

    success = await db_delete_single_note("telegram", chat_id, note_id)
    if success:
        await update.message.reply_text(f"🗑️ Đã xóa thành công bài học **[#{note_id}]** khỏi hệ thống!", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Không tìm thấy bài học **[#{note_id}]**.", parse_mode="Markdown")


async def xoatatca_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    arg = context.args[0].lower().strip() if context.args else ""

    if arg != "xacnhan":
        msg = (
            "⚠️ **CẢNH BÁO XÓA BỘ NHỚ!**\n\n"
            "Lệnh này sẽ **xóa sạch toàn bộ bài học, tệp và ảnh** trên Cloud.\n\n"
            "👉 Vui lòng gõ đúng cú pháp để xác nhận:\n"
            "`/xoatatca xacnhan`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    success = await db_delete_user_data("telegram", chat_id)
    if success:
        await update.message.reply_text(
            "🧹 **Đã xóa sạch toàn bộ dữ liệu bài học trên Cloud!**\n"
            "Bộ nhớ đã được làm mới hoàn toàn. Bạn có thể dùng `/save` để bắt đầu lại! 🚀",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Lỗi khi thực hiện xóa dữ liệu.")


async def telegram_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_str = query.data
    chat_id = str(update.effective_chat.id)

    if data_str.startswith("recall_answer_"):
        n_id = int(data_str.split("_")[-1])
        ACTIVE_RECALL_SESSIONS[chat_id] = n_id
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=(
                f"✍️ **Đang chờ câu trả lời cho bài [#{n_id}]!**\n\n"
                "👉 Bạn hãy gửi câu trả lời bằng **tin nhắn văn bản** hoặc **bấm giữ ghi âm Voice (giọng nói)**.\n"
                "Groq AI sẽ tự động lắng nghe, đối chiếu với kiến thức gốc và chấm điểm cho bạn! 🎯"
            ),
            parse_mode="Markdown"
        )

    elif data_str.startswith("recall_ok_"):
        n_id = int(data_str.split("_")[-1])
        msg = await db_update_progress(n_id, remembered=True)
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=int(chat_id), text=msg)

    elif data_str.startswith("recall_retry_"):
        n_id = int(data_str.split("_")[-1])
        msg = await db_update_progress(n_id, remembered=False)
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=int(chat_id), text=msg)


async def handle_telegram_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    voice = update.message.voice
    if not voice:
        return

    status_msg = await update.message.reply_text("🎙️ Đang nghe và chuyển đổi giọng nói của bạn qua Whisper AI...")
    try:
        tg_file = await voice.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        audio_bytes = buf.getvalue()

        transcribed_text = await ask_ai_transcribe_audio(audio_bytes)
        if not transcribed_text:
            await status_msg.edit_text("⚠️ Không nhận diện được giọng nói hoặc âm thanh quá nhỏ.")
            return

        await status_msg.edit_text(f"🗣️ *Bạn đã nói:* \"{transcribed_text}\"\n\n🤖 Đang đối chiếu bài học và chấm điểm...", parse_mode="Markdown")

        if chat_id in ACTIVE_RECALL_SESSIONS:
            target_id = ACTIVE_RECALL_SESSIONS.pop(chat_id)
            note = await db_get_note_by_id(target_id)
            if note:
                feedback, score = await evaluate_active_recall_answer(note["content"], transcribed_text)
                is_passed = score >= 7
                progress_msg = await db_update_progress(target_id, remembered=is_passed)
                
                score_badge = "🌟 ĐẠT YÊU CẦU" if is_passed else "🔄 CẦN CỦNG CỐ THÊM"
                reply_text = (
                    f"🎯 **KẾT QUẢ CHẤM ĐIỂM ACTIVE RECALL [Bài #{target_id}]**\n"
                    f"📊 **Điểm số:** `{score}/10` ({score_badge})\n\n"
                    f"{feedback}\n\n"
                    f"📈 **Cập nhật lộ trình:** {progress_msg}"
                )
                await update.message.reply_text(reply_text)
                return

        recent_notes = await db_get_user_notes("telegram", chat_id, limit=10)
        context_data = ""
        if recent_notes:
            for n in recent_notes:
                context_data += f"- [Mã #{n['id']}]: {n['content']}\n"
        prompt = f"Người dùng nói: '{transcribed_text}'. Hãy phản hồi thân thiện dựa trên kho kiến thức sau nếu liên quan:\n{context_data}"
        ans = await ask_ai(prompt)
        await update.message.reply_text(ans)
    except Exception as e:
        logger.error(f"Lỗi voice Telegram: {e}")
        await status_msg.edit_text(f"❌ Lỗi xử lý âm thanh: {e}")


async def handle_chat_with_groq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    raw_name = update.effective_user.first_name or "Phong"
    call_name = "Phong" if "phong" in raw_name.lower() or raw_name in ["Bạn", "User"] else raw_name

    # 1. Tiếp nhận câu trả lời Active Recall dạng văn bản
    if chat_id in ACTIVE_RECALL_SESSIONS:
        target_id = ACTIVE_RECALL_SESSIONS.pop(chat_id)
        note = await db_get_note_by_id(target_id)
        if note:
            await update.message.chat.send_action("typing")
            feedback, score = await evaluate_active_recall_answer(note["content"], user_text)
            is_passed = score >= 7
            progress_msg = await db_update_progress(target_id, remembered=is_passed)
            score_badge = "🌟 ĐẠT YÊU CẦU" if is_passed else "🔄 CẦN CỦNG CỐ THÊM"

            reply_text = (
                f"🎯 **KẾT QUẢ CHẤM ĐIỂM ACTIVE RECALL [Bài #{target_id}]**\n"
                f"📊 **Điểm số:** `{score}/10` ({score_badge})\n\n"
                f"{feedback}\n\n"
                f"📈 **Cập nhật lộ trình:** {progress_msg}"
            )
            await update.message.reply_text(reply_text)
            return

    # 2. Xử lý hội thoại tự nhiên thông thường
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
        context_data = "KHO BÀI HỌC/KIẾN THỨC ĐÃ LƯU TRÊN HỆ THỐNG:\n"
        for n in recent_notes:
            c_date = n["created_at"].strftime("%d/%m/%Y") if n.get("created_at") else "Gần đây"
            f_note = f" (Có tệp gốc: {n['file_name']})" if n.get("file_id") else ""
            t_note = f" [Thẻ: {' '.join(n.get('tags', []))}]" if n.get("tags") else ""
            r_note = f" [Hẹn: {n['remind_at'].astimezone(LOCAL_TZ).strftime('%H:%M %d/%m')}]" if n.get("remind_at") else ""
            context_data += f"- [Mã #{n['id']}]{t_note}{r_note} (Ngày {c_date}){f_note}: {n['content']}\n"
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
        f"2. Nếu {call_name} muốn đặt giờ nhắc (ví dụ 'nhắc tôi bài 8 lúc 3h chiều'), hãy hướng dẫn cú pháp nhanh: /nhac <id> <giờ>.\n"
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
            note_id = await db_save_note(
                platform="telegram",
                user_id=chat_id,
                content=f"Hình ảnh: {file_name}\n{analysis}",
                cycle="hangngay",
                cycle_label="Hình ảnh tài liệu",
                target_date=datetime.now(LOCAL_TZ).date(),
                file_id=doc.file_id,
                file_name=file_name,
                tags=["#hinhanh", "#tailieu"]
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
        doc_tags = [f"#{ext}"]
        note_id = await db_save_note(
            platform="telegram",
            user_id=chat_id,
            content=f"Tài liệu {file_name}:\n{analysis}",
            cycle="hangngay",
            cycle_label="Tài liệu văn phòng",
            target_date=datetime.now(LOCAL_TZ).date(),
            file_id=doc.file_id,
            file_name=file_name,
            tags=doc_tags
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
        note_id = await db_save_note(
            platform="telegram",
            user_id=chat_id,
            content=f"Ảnh chụp ghi chú:\n{analysis}",
            cycle="hangngay",
            cycle_label="Ảnh chụp tài liệu",
            target_date=datetime.now(LOCAL_TZ).date(),
            file_id=photo.file_id,
            file_name="photo.jpg",
            tags=["#anhchup", "#ocr"]
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


async def send_lark_interactive_card(open_id: str, title: str, content: str, note_id: int, header_color: str = "blue"):
    """
    Gửi thẻ tương tác Lark. 
    header_color: 'red' (cảnh báo/ưu tiên cao), 'blue' (thông thường), 'orange' (nhắc nhở).
    """
    token = await get_lark_tenant_token()
    if not token:
        return

    card_content = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": header_color,
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
        doc_tags = [f"#{ext}"]
        note_id = await db_save_note(
            platform="lark",
            user_id=open_id,
            content=f"Tài liệu {file_name}:\n{analysis}",
            cycle="hangngay",
            cycle_label="Tài liệu văn phòng",
            target_date=datetime.now(LOCAL_TZ).date(),
            file_id=file_key,
            file_name=file_name,
            tags=doc_tags
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
            file_name="image.jpg",
            tags=["#anhchup", "#ocr"]
        )
        await send_lark_message(open_id, f"🖼️ **KẾT QUẢ ĐỌC HÌNH ẢNH [Mã #{note_id}]:**\n\n{analysis}")
        return

    raw = content_json.get("text", "").strip()
    if not raw:
        return

    if open_id in ACTIVE_RECALL_SESSIONS:
        target_id = ACTIVE_RECALL_SESSIONS.pop(open_id)
        note = await db_get_note_by_id(target_id)
        if note:
            feedback, score = await evaluate_active_recall_answer(note["content"], raw)
            is_passed = score >= 7
            progress_msg = await db_update_progress(target_id, remembered=is_passed)
            score_badge = "🌟 ĐẠT YÊU CẦU" if is_passed else "🔄 CẦN CỦNG CỐ THÊM"
            res = (
                f"🎯 **KẾT QUẢ CHẤM ĐIỂM ACTIVE RECALL [Bài #{target_id}]**\n"
                f"📊 **Điểm số:** `{score}/10` ({score_badge})\n\n"
                f"{feedback}\n\n"
                f"📈 **Cập nhật lộ trình:** {progress_msg}"
            )
            await send_lark_message(open_id, res)
            return

    if raw.lower() in ["/start", "hi", "hello", "chào", "chao"]:
        reply = "Chào Phong! 🌟 Chúc bạn một ngày tràn đầy năng lượng và cảm hứng học tập! 🚀💡\n\n" + HELP_MESSAGE
        await send_lark_message(open_id, reply)
    elif raw.lower() in ["/help", "/menu", "/lenh"]:
        await send_lark_message(open_id, HELP_MESSAGE)
    elif raw.startswith("/save"):
        reply = await handle_save_command_core("lark", open_id, raw[5:].strip())
        await send_lark_message(open_id, reply)
    elif raw.startswith("/nhac"):
        reply = await handle_nhac_command_core("lark", open_id, raw[5:].strip())
        await send_lark_message(open_id, reply)
    elif raw.startswith("/list"):
        parts = raw.split()
        tag_filter = parts[1] if len(parts) > 1 else None
        reply = await handle_list_notes_core("lark", open_id, tag_filter)
        await send_lark_message(open_id, reply)
    elif raw.startswith("/search"):
        reply = await handle_search_notes_core("lark", open_id, raw[7:].strip())
        await send_lark_message(open_id, reply)
    elif raw.startswith("/ontap"):
        reply, note_id = await handle_ontap_command_core("lark", open_id, raw[6:].strip())
        if note_id:
            ACTIVE_RECALL_SESSIONS[open_id] = note_id
            await send_lark_interactive_card(open_id, f"📚 Ôn tập bài #{note_id}", reply + "\n\n*(Nhắn lại câu trả lời vào đây để AI chấm điểm)*", note_id, header_color="blue")
        else:
            await send_lark_message(open_id, reply)
    elif raw.startswith("/xoa ") or raw.startswith("/del "):
        parts = raw.split()
        if len(parts) > 1 and parts[1].isdigit():
            target_id = int(parts[1])
            success = await db_delete_single_note("lark", open_id, target_id)
            if success:
                await send_lark_message(open_id, f"🗑️ Đã xóa bài học [#{target_id}] khỏi hệ thống!")
            else:
                await send_lark_message(open_id, f"❌ Không tìm thấy bài học [#{target_id}].")
        else:
            await send_lark_message(open_id, "⚠️ Cú pháp: /xoa <id> (Ví dụ: /xoa 3)")
    elif raw.startswith("/xoatatca") or raw.startswith("/xoahet"):
        parts = raw.split()
        if len(parts) > 1 and parts[1].lower() == "xacnhan":
            await db_delete_user_data("lark", open_id)
            await send_lark_message(open_id, "🧹 Đã xóa sạch toàn bộ dữ liệu bài học trên Cloud!")
        else:
            await send_lark_message(open_id, "⚠️ Để xóa sạch bài học, hãy gõ chính xác: /xoatatca xacnhan")
    else:
        recent_notes = await db_get_user_notes("lark", open_id, limit=10)
        context_data = ""
        if recent_notes:
            context_data = "KHO BÀI HỌC/KIẾN THỨC ĐÃ LƯU TRÊN HỆ THỐNG:\n"
            for n in recent_notes:
                c_date = n["created_at"].strftime("%d/%m/%Y") if n.get("created_at") else "Gần đây"
                f_note = f" (Có tệp gốc: {n['file_name']})" if n.get("file_id") else ""
                t_note = f" [Thẻ: {' '.join(n.get('tags', []))}]" if n.get("tags") else ""
                r_note = f" [Hẹn: {n['remind_at'].astimezone(LOCAL_TZ).strftime('%H:%M %d/%m')}]" if n.get("remind_at") else ""
                context_data += f"- [Mã #{n['id']}]{t_note}{r_note} (Ngày {c_date}){f_note}: {n['content']}\n"
        else:
            context_data = "Chưa có bài học nào được lưu.\n"

        prompt = (
            f"Bạn là người bạn trợ lý học tập thân thiết của anh Phong.\n"
            f"Dưới đây là các bài học Phong đã lưu:\n{context_data}\n\n"
            f"Hãy giải đáp tin nhắn của Phong, nếu liên quan bài học cũ hãy trích xuất chính xác kèm mã bài học (#ID). "
            f"Nếu Phong muốn đặt giờ nhắc, hướng dẫn dùng lệnh /nhac <id> <giờ>. "
            f"Xưng hô tự nhiên, gọi tên Phong, dùng emoji vui vẻ (🌟, 🚀, 💡): '{raw}'"
        )
        reply = await ask_ai(prompt)
        await send_lark_message(open_id, reply)


# --- BỘ LẬP LỊCH TỰ ĐỘNG & PUSH NOTIFICATION ƯU TIÊN CAO (QUÉT MỖI PHÚT) ---

async def dynamic_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(LOCAL_TZ)
    current_time_str = now.strftime("%H:%M")
    today_date = now.date()

    # 1. Quét các bài học có HẸN GIỜ TÙY CHỌN (remind_at) -> PUSH NOTIFICATION KHẨN CẤP
    custom_due = await db_get_custom_remind_notes(now)
    for note in custom_due:
        n_id = note["id"]
        platform = note["platform"]
        user_id = note["user_id"]
        
        prompt = (
            f"Bài học: '{note['content']}'.\n"
            "Hãy tạo 1 câu hỏi ôn tập Active Recall ngắn gọn bằng tiếng Việt để kiểm tra phản xạ ngay lúc này."
        )
        ai_q = await ask_ai(prompt)
        title = "🚨 **CẢNH BÁO NHẮC NHỞ HẸN GIỜ!** 🔔⚡"
        body = (
            f"{title}\n\n"
            f"📖 *Nội dung bài #{n_id}:* {note['content']}\n\n"
            f"{ai_q}"
        )

        try:
            if platform == "telegram":
                # Push Notification mức cao: Bật chuông/rung và tự động Ghim tin nhắn
                sent_msg = await context.bot.send_message(
                    chat_id=int(user_id),
                    text=body,
                    reply_markup=make_recall_keyboard(n_id),
                    parse_mode="Markdown",
                    disable_notification=False  # Buộc phát chuông và rung
                )
                try:
                    await context.bot.pin_chat_message(
                        chat_id=int(user_id),
                        message_id=sent_msg.message_id,
                        disable_notification=False
                    )
                except Exception as pin_err:
                    logger.warning(f"Không thể ghim tin nhắn #{n_id}: {pin_err}")
            else:
                # Lark Suite: Đẩy thẻ cảnh báo màu đỏ (Red Template)
                await send_lark_interactive_card(user_id, title, body, n_id, header_color="red")
            
            # Sau khi nhắc xong, hủy mốc hẹn giờ để không nhắc lặp lại
            await db_update_remind_time(n_id, None)
        except Exception as e:
            logger.error(f"Lỗi gửi thông báo hẹn giờ bài #{n_id}: {e}")

    # 2. Khung giờ nhắc tổng kết đêm 22:00
    if current_time_str == "22:00":
        msg = (
            "🌙 **Đến giờ tổng kết kiến thức ngày hôm nay!** 🌟✨\n\n"
            "Hôm nay bạn học được bài học mới nào? Hãy gửi ngay cú pháp:\n"
            "`/save [chu_kỳ] [#nhãn] <nội dung>` hoặc gửi file tài liệu để lưu vào Supabase Cloud nhé!"
        )
        try:
            if DB_POOL:
                async with DB_POOL.acquire() as conn:
                    users = await conn.fetch("SELECT DISTINCT platform, user_id FROM user_notes;")
                    for u in users:
                        p, uid = u["platform"], u["user_id"]
                        if p == "telegram":
                            await context.bot.send_message(chat_id=int(uid), text=msg, parse_mode="Markdown", disable_notification=False)
                        else:
                            await send_lark_message(uid, msg)
        except Exception as e:
            logger.error(f"Lỗi gửi nhắc đêm: {e}")
        return

    # 3. Khung giờ nhắc Spaced Repetition cố định: 10:00 sáng & 19:30 tối
    is_morning = (current_time_str == "10:00")
    is_evening = (current_time_str == "19:30")
    if not (is_morning or is_evening):
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
                sent_msg = await context.bot.send_message(
                    chat_id=int(user_id),
                    text=body,
                    reply_markup=make_recall_keyboard(n_id),
                    parse_mode="Markdown",
                    disable_notification=False
                )
                try:
                    await context.bot.pin_chat_message(chat_id=int(user_id), message_id=sent_msg.message_id, disable_notification=False)
                except Exception:
                    pass
            else:
                await send_lark_interactive_card(user_id, title, body, n_id, header_color="orange")
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
        BotCommand("save", "Lưu bài học (/save 14:30 #tag ...)"),
        BotCommand("nhac", "Đặt giờ nhắc cho bài học (/nhac <id> <giờ>)"),
        BotCommand("ontap", "Ôn tập kiến thức & AI chấm điểm"),
        BotCommand("list", "Xem danh sách hoặc lọc nhãn (/list #nhan)"),
        BotCommand("search", "Tìm bài học cũ theo từ khóa"),
        BotCommand("getfile", "Tải lại tệp gốc đính kèm (/getfile <id>)"),
        BotCommand("xoa", "Xóa 1 bài học theo mã ID (/xoa <id>)"),
        BotCommand("xoatatca", "Xóa sạch bộ nhớ bài học (/xoatatca xacnhan)"),
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
    application.add_handler(CommandHandler("nhac", nhac_command))
    application.add_handler(CommandHandler("ontap", ontap_command))
    application.add_handler(CommandHandler("list", list_notes_command))
    application.add_handler(CommandHandler("search", search_notes_command))
    application.add_handler(CommandHandler("getfile", getfile_command))
    application.add_handler(CommandHandler("xoa", xoa_command))
    application.add_handler(CommandHandler("del", xoa_command))
    application.add_handler(CommandHandler("xoatatca", xoatatca_command))
    application.add_handler(CommandHandler("xoahet", xoatatca_command))
    application.add_handler(CallbackQueryHandler(telegram_callback_handler))

    # Đăng ký nhận voice, ảnh, file, text
    application.add_handler(MessageHandler(filters.VOICE, handle_telegram_voice))
    application.add_handler(MessageHandler(filters.PHOTO, telegram_handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, telegram_handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_with_groq))

    # Job Queue quét kiểm tra mỗi 60 giây
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
