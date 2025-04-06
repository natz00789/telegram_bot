from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import Updater, MessageHandler, Filters, CallbackContext, CommandHandler, CallbackQueryHandler
from datetime import datetime, timedelta
import logging
import threading
import re
import os
import time
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from collections import defaultdict
import sqlite3  # Import SQLite
from enum import Enum  # Import Enum
from difflib import SequenceMatcher
import base64

# ==== CONFIG ==== #
TOKEN = "7625175226:AAGqGqNGpQWr0oK-7RQyagOeNEf3meWsv28"

GROUP_RECEIVE_ORDER = -4791349620
GROUP_PACK = -4764678994
GROUP_DROP = -4603425822
GROUP_FINISH = -4688273765
GROUP_BILL = -4640161680

order_counter = 1
order_history = []
daily_orders = []
delivery_logs = []

job_status = {}
job_details = {}
user_jobs = defaultdict(lambda: defaultdict(list))
pending_orders_by_user = {}
pending_orders_lock = threading.Lock()  # Lock for pending orders

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')  # Improved Logging

# ==== GOOGLE SHEETS SETUP ==== #
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
service_account_base64 = os.getenv("GOOGLE_SERVICE_JSON_BASE64")
if not service_account_base64:
    raise ValueError("ไม่พบ GOOGLE_SERVICE_JSON_BASE64 ใน environment")

decoded_bytes = base64.b64decode(service_account_base64)
decoded_text = decoded_bytes.decode("utf-8")
service_account_info = json.loads(decoded_text)# type: ignore

creds = ServiceAccountCredentials.from_json_keyfile_dict(service_account_info, scope)
client = gspread.authorize(creds)
sheet = client.open("Telegram Orders Log")

# ==== SHEETS LOGGING ==== #
def log_daily_order(date_str, count, total):
    try:
        ws = sheet.worksheet("DailyOrders")
        ws.append_row([date_str, count, total])
    except Exception as e:
        logging.error(f"Error logging daily order: {e}")  # Log errors

def log_customer_info(date_str, order_id, user, text):
    try:
        ws = sheet.worksheet("CustomerInfo")
        ws.append_row([date_str, order_id, user, text])
    except Exception as e:
        logging.error(f"Error logging customer info: {e}")

def log_finished_job(date_str, driver, order_id, price, file_id):
    try:
        ws = sheet.worksheet("FinishedJobs")
        ws.append_row([date_str, driver, order_id, price, file_id])
    except Exception as e:
        logging.error(f"Error logging finished job: {e}")

def log_driver_summary(date_str, summary_text):
    try:
        ws = sheet.worksheet("DriverSummary")
        ws.append_row([date_str, summary_text])
    except Exception as e:
        logging.error(f"Error logging driver summary: {e}")

# ==== STOCK MANAGEMENT (IMPROVED PARSER) ==== #


# แผนที่แบรนด์กับหมวดหมู่ (เพิ่มแบรนด์ได้ที่นี่)
BRAND_CATEGORY_MAP = {
    'relx': 'หัว',
    'relx3.2': 'หัว',
    'infyplus': 'หัว',
    'jues': 'หัว',
    'marbo': 'หัว',
    'hyper': 'หัว',
    'flow': 'หัว',
    'mb9k': 'ใช้แล้วทิ้ง',
    'mb9kaaa': 'ใช้แล้วทิ้ง',
    'akbar': 'ใช้แล้วทิ้ง',
    'bigdic': 'ใช้แล้วทิ้ง',
    'elfbar10k': 'ใช้แล้วทิ้ง',
    'ks6k': 'ใช้แล้วทิ้ง',
    'nexo3k': 'ใช้แล้วทิ้ง',
    'novo14k': 'ใช้แล้วทิ้ง',
    'infy12k': 'ใช้แล้วทิ้ง',
    'nexo12k': 'ใช้แล้วทิ้ง',
    'eskobar20k': 'ใช้แล้วทิ้ง',
    'relx2plus': 'เครื่อง',
    'fitpod': 'เครื่อง',
    'cube': 'เครื่อง',
}

def normalize_string(text):
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[\u200b-\u200f\uFEFF]", "", text)
    return text

def detect_category(brand):
    for keyword, cat in BRAND_CATEGORY_MAP.items():
        if keyword in brand:
            return cat
    return 'ไม่ทราบ'

def is_brand_line(line):
    """เช็กว่าเป็นบรรทัดที่น่าจะเป็นชื่อยี่ห้อ (แม้มีเลข)"""
    line = normalize_string(line)
    return any(k in line for k in BRAND_CATEGORY_MAP.keys())

def is_ignored_line(line):
    """กรองบรรทัดที่ไม่ควรนำมาเป็นรายการสินค้า"""
    keywords = ['ค่าส่ง', 'maps', 'Bkk-', 'ที่อยู่','เบอร์', 'http', 'โทร', 'Rst-', 'Rf-', 'Bl-', 'ปาล์ม', 'จัสเอ']
    return any(keyword in line.lower() for keyword in keywords)

def parse_order_items(text):
    lines = text.splitlines()
    items = []
    current_brand = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if is_ignored_line(line):
            continue

        match_inline = re.match(r'([\w\d\.]+)\s+(.*?)\s+(\d+)$', line)
        if match_inline:
            brand = normalize_string(match_inline.group(1))
            current_brand = brand
            flavor = normalize_string(match_inline.group(2))
            quantity = int(match_inline.group(3))
        elif is_brand_line(line):
            brand = normalize_string(line)
            current_brand = brand
            continue
        elif re.search(r'\d+$', line):
            if current_brand is None:
                logging.warning(f"ข้ามบรรทัด (ไม่มียี่ห้อ): {line}")
                continue
            if is_ignored_line(line):
                continue
            parts = line.rsplit(' ', 1)
            if len(parts) != 2:
                continue
            flavor = normalize_string(parts[0])
            try:
                quantity = int(parts[1])
            except ValueError:
                logging.error(f"Invalid quantity format: {parts[1]}")
                continue
        else:
            continue

        category = detect_category(current_brand)
        items.append({
            'หมวดหมู่': category,
            'ยี่ห้อ': current_brand,
            'กลิ่น': flavor,
            'จำนวน': quantity
        })

    logging.debug(f"รายการสินค้า: {items}")
    return items

def check_stock(items):
    try:
        ws = sheet.worksheet("Stock")
        data = ws.get_all_records(expected_headers=['หมวดหมู่', 'ยี่ห้อ', 'กลิ่น', 'คงเหลือ'])
        logging.debug(f"ข้อมูลสต็อกจากชีต: {data}")
        not_available = []
        for item in items:
            found = False
            for row in data:
                # เพิ่มการเปรียบเทียบหมวดหมู่ (ถ้ามี) และยี่ห้ออย่างละเอียด
                row_category = normalize_string(row['หมวดหมู่'])
                item_category = normalize_string(item['หมวดหมู่'])
                row_brand = normalize_string(row['ยี่ห้อ'])
                item_brand = normalize_string(item['ยี่ห้อ'])
                row_flavor = normalize_string(row['กลิ่น'])
                item_flavor = normalize_string(item['กลิ่น'])
                
                # Fuzzy String Matching สำหรับ "กลิ่น"
                                
                if (row_category == item_category and
                    row_brand == item_brand and
                    row_flavor == item_flavor):  # กำหนดเกณฑ์ความคล้ายคลึง
                    logging.debug(f"ตรวจเจอสินค้าในชีต: {item}")
                    if int(row['คงเหลือ']) < item['จำนวน']:
                        item['เหตุผล'] = 'จำนวนไม่พอ'
                        not_available.append(item)
                    found = True
                    break
            if not found:
                item['เหตุผล'] = 'ไม่พบในสต็อก'
                not_available.append(item)
        logging.debug(f"สินค้าที่ไม่พร้อมขาย: {not_available}")
        return not_available
    except Exception as e:
        logging.error(f"Error checking stock: {e}")
        return []

def update_stock(items):
    try:
        ws = sheet.worksheet("Stock")
        data = ws.get_all_records(expected_headers=['หมวดหมู่', 'ยี่ห้อ', 'กลิ่น', 'คงเหลือ'])
        for idx, row in enumerate(data):
            for item in items:
                row_category = normalize_string(row['หมวดหมู่'])
                item_category = normalize_string(item['หมวดหมู่'])
                row_brand = normalize_string(row['ยี่ห้อ'])
                item_brand = normalize_string(item['ยี่ห้อ'])
                row_flavor = normalize_string(row['กลิ่น'])
                item_flavor = normalize_string(item['กลิ่น'])

                
                if (row_category == item_category and
                    row_brand == item_brand and
                    row_flavor == item_flavor):
                    try:
                        new_qty = max(0, int(row['คงเหลือ']) - item['จำนวน'])
                        ws.update_cell(idx + 2, 4, new_qty)
                        logging.debug(f"อัปเดตสต็อก: {item} → เหลือ {new_qty}")
                    except Exception as e:
                        logging.error(f"Error updating stock for item: {item} - {e}")
                    break
    except Exception as e:
        logging.error(f"Error updating stock: {e}")

# ==== UTILS ==== #
ORDER_ID_FILE = "order_id.txt"

def load_order_counter():
    global order_counter
    try:
        if os.path.exists(ORDER_ID_FILE):
            with open(ORDER_ID_FILE, "r") as f:
                order_counter = int(f.read().strip())
        else:
            order_counter = 1
    except Exception as e:
        logging.error(f"Error loading order counter: {e}")

def save_order_counter():
    try:
        with open(ORDER_ID_FILE, "w") as f:
            f.write(str(order_counter))
    except Exception as e:
        logging.error(f"Error saving order counter: {e}")


def is_order_complete(text):
    has_order = "order" in text.lower()

    # 🔁 เปลี่ยนจากตรวจ "รหัส" เป็นเช็ก prefix ใหม่แทน
    has_customer_code = any(re.search(rf"\b{prefix}", text, re.IGNORECASE) for prefix in [
        'Bkk-', 'Rst-', 'Rf-', 'Bl-', 'ปาล์ม', 'จัสเอ'
    ])

    has_phone = bool(re.search(r"0[689]\d{8}", text))
    has_shipping = "ค่าส่ง" in text
    has_address = "maps.app.goo.gl" in text or "ที่อยู่" in text

    is_phone_valid = False
    if has_phone:
        phone_match = re.search(r"0[689]\d{8}", text)
        if phone_match:
            phone_number = phone_match.group(0)
            is_phone_valid = len(phone_number) == 10

    return all([has_order, has_customer_code, is_phone_valid, has_shipping, has_address])


def extract_price(text):
    try:
        match = re.search(r'ค่าส่ง\s*[:=\-]?\s*(\d+)', text)
        if match:
            return int(match.group(1))
    except:
        return 0
    return 0

def extract_essential_info(text):
    lines = text.splitlines()
    result = []
    for line in lines:
        if any(word in line for word in ["รหัส", "Bkk-", "Rst-", "Rf-", "Bl-", "ปาล์ม", "จัสเอ", "Nh"]):
            result.append(line.strip())
        elif re.search(r"0[689]\d{8}", line):
            result.append(line.strip())
        elif "ค่าส่ง" in line:
            result.append(line.strip())
        elif "maps.app.goo.gl" in line or "ที่อยู่" in line:
            result.append(line.strip())
    return "\n".join(result)

def is_similar(a, b):
    return SequenceMatcher(None, a, b).ratio()

def extract_order_signature(text):
    """ตัดข้อความให้เหลือเฉพาะข้อมูลสำคัญที่ใช้เปรียบเทียบ"""
    lines = text.splitlines()
    essentials = []
    for line in lines:
        if any(k in line.lower() for k in ["bkk-", "rst-", "rf-", "bl-", "ปาล์ม", "จัสเอ", "ที่อยู่", "maps", "ค่าส่ง"]) or re.search(r"0[689]\d{8}", line):
            essentials.append(normalize_string(line))
        elif re.search(r'\d+$', line) and not is_ignored_line(line):
            essentials.append(normalize_string(line))
    return "\n".join(essentials)

def is_duplicate_order(new_text):
    new_sig = extract_order_signature(new_text)
    for old_text in order_history[-30:]:  # เช็กเฉพาะออเดอร์ล่าสุด 30 รายการ
        old_sig = extract_order_signature(old_text)
        similarity = is_similar(new_sig, old_sig)
        if similarity >= 0.75:
            return True, similarity
    return False, 0.0


# ==== HANDLE ORDER W/ STOCK CHECK ====
def handle_order(update: Update, context: CallbackContext):
    global pending_orders_by_user

    message = update.message
    text = message.text
    user_id = message.from_user.id
    user = message.from_user.first_name

    if message.chat_id != GROUP_RECEIVE_ORDER:
        return

    if not is_order_complete(text):
        message.reply_text("❌ ข้อมูลไม่ครบ ต้องมี 'รหัส', 'เบอร์โทร', 'ค่าส่ง', 'ที่อยู่'")
        return

# ==== ตรวจออเดอร์ซ้ำ ====
    duplicate, similarity = is_duplicate_order(text)
    if duplicate:
        message.reply_text(f"⚠️ ตรวจพบว่าออเดอร์นี้คล้ายกับออเดอร์ก่อนหน้า ({similarity*100:.1f}%)\nหากต้องการสั่งซ้ำจริง กรุณาแก้ไขข้อความเล็กน้อยหรือรออีกสักครู่")
        return
    
    # ==== เช็กสต็อกจาก Google Sheets ====
    items = parse_order_items(text)
    logging.debug(f"รายการสินค้า: {items}")  # Use logging.debug
    not_ready = check_stock(items)
    if not_ready:
        msg = "❌ สินค้าบางรายการไม่พร้อมขาย:\n"
        for i in not_ready:
            msg += f"- {i['หมวดหมู่']} {i['ยี่ห้อ']} กลิ่น {i['กลิ่น']} ({i['จำนวน']}) [{i['เหตุผล']}]\n"
        sent = message.reply_text(msg)
        context.job_queue.run_once(lambda ctx: ctx.bot.delete_message(chat_id=sent.chat_id, message_id=sent.message_id), when=300)
        return

    with pending_orders_lock:
        if user_id in pending_orders_by_user:
            message.reply_text("❌ กรุณาแนบสลิปให้กับออเดอร์ก่อนหน้าก่อนส่งข้อความอื่น")
            return

        pending_orders_by_user[user_id] = {
            'text': text,
            'name': user,
            'timestamp': time.time(),
            'items': items
        }
    sent = message.reply_text("🕒 รอแนบสลิปภายใน 3 นาที...")
    context.job_queue.run_once(lambda ctx: ctx.bot.delete_message(chat_id=sent.chat_id, message_id=sent.message_id), when=300)

# ==== HANDLE SLIP W/ STOCK UPDATE ====
def handle_order_slip(update: Update, context: CallbackContext):
    global pending_orders_by_user, order_counter

    message = update.message
    user_id = message.from_user.id

    if message.chat_id != GROUP_RECEIVE_ORDER:
        return

    with pending_orders_lock:
        if user_id not in pending_orders_by_user:
            return

        pending = pending_orders_by_user.pop(user_id)

    text = pending['text']
    user = pending['name']
    items = pending.get('items', [])

    if time.time() - pending['timestamp'] > 180:
        message.reply_text("⏰ หมดเวลารอแนบสลิปแล้ว กรุณาส่งออเดอร์ใหม่อีกครั้ง")
        return

    update_stock(items)

    order_id = f"{order_counter:02d}"
    order_counter += 1
    save_order_counter()
    order_history.append(text)
    daily_orders.append(datetime.now().date())

    price = extract_price(text)
    if price >= 20:
        new_price = price - 20
        text = text.replace(str(price), str(new_price))

    today_str = datetime.now().strftime("%d/%m/%Y")
    new_text = f'''📦 <b>ออเดอร์ใหม่</b> #ORDER{order_id} ({today_str}) โดย {user}\n\n{text}'''

    
    sent = context.bot.send_message(chat_id=GROUP_PACK, text=new_text, parse_mode=ParseMode.HTML)
    message.reply_text(f"✅ ส่งออเดอร์ไปกลุ่มแพ็คงานแล้ว #ORDER{order_id}")
    # ==== เก็บข้อมูลลงชีทแบบละเอียด ====
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    customer_code_match = re.search(r"(Bkk-|Rst-|Rf-|Bl-|ปาล์ม|จัสเอ)\S*", text)
    customer_code = customer_code_match.group(0) if customer_code_match else "ไม่พบ"

    address_match = re.search(r"(ที่อยู่[^\n]*|https://maps\.app\.goo\.gl\S*)", text)
    address = address_match.group(0) if address_match else "ไม่พบ"

    total_match = re.search(r"=\s*([\d,]+)", text)
    try:
        total_cost = int(total_match.group(1).replace(",", "")) if total_match else price
    except:
        total_cost = price

    log_order_details(timestamp, customer_code, items, address, price, total_cost)

    log_customer_info(
    datetime.now().strftime("%Y-%m-%d"),
    f"ORDER{order_id}",
    user,
    text
)


def log_order_details(timestamp, customer_code, items, address, shipping_cost, total_cost):
    try:
        ws = sheet.worksheet("OrderDetailsLog")
        first_row = True
        for item in items:
            ws.append_row([
                timestamp,
                customer_code,
                item['ยี่ห้อ'],
                item['กลิ่น'],
                item['จำนวน'],
                address,
                shipping_cost if first_row else '',
                total_cost if first_row else ''
            ])
            first_row = False
    except Exception as e:
        logging.error(f"Error logging order details: {e}")

def button_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    user = query.from_user.first_name
    data = query.data

    if data.startswith("รับงาน_"):
        job_id = data.replace("รับงาน_", "")
        price = extract_price(query.message.text or query.message.caption)
        context.bot.send_message(chat_id=GROUP_DROP, text=f"🚚 {user} รับงาน #ORDER{job_id} แล้ว ค่าส่ง {price}")
        query.answer("รับงานแล้ว")
        query.edit_message_reply_markup(reply_markup=None)

    
        
# ==== CLEANUP PENDING ORDERS ==== #
def clean_pending_orders():
    while True:
        now = time.time()
        with pending_orders_lock:
            for user_id in list(pending_orders_by_user.keys()):
                if now - pending_orders_by_user[user_id]['timestamp'] > 180:
                    del pending_orders_by_user[user_id]
        time.sleep(60)


def handle_pack_reply_photo(update: Update, context: CallbackContext):
    message = update.message
    if message.chat_id != GROUP_PACK or not message.reply_to_message:
        return

    # ตรวจว่ารูปถูก reply จากข้อความออเดอร์
    original_text = message.reply_to_message.text or message.reply_to_message.caption
    if not original_text or '#ORDER' not in original_text:
        return

    essential_text = extract_essential_info(original_text)
    order_id_match = re.search(r'#ORDER(\d+)', original_text)
    order_id = order_id_match.group(1) if order_id_match else 'ไม่พบเลข'

    keyboard = [[InlineKeyboardButton("✅ รับงาน", callback_data=f"รับงาน_{order_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    summary = f"#ORDER{order_id}\n{essential_text}"
    context.bot.send_message(chat_id=GROUP_DROP, text=summary, reply_markup=reply_markup)
    message.reply_text("✅ ส่งไปยังกลุ่มปล่อยงานแล้ว")


# ==== DROP GROUP ==== #
def handle_drop_reply(update: Update, context: CallbackContext):
    message = update.message
    if message.chat_id != GROUP_DROP or not message.reply_to_message:
        return

    if message.photo:
        price = extract_price(message.reply_to_message.text)
        order_match = re.search(r'#ORDER(\d+)', message.reply_to_message.text)
        order_id = order_match.group(0) if order_match else 'ไม่พบเลข'
        caption = f"📷 งานจบแล้ว ค่าส่ง {price} โดย {message.from_user.first_name}\n\n{message.reply_to_message.text}"
        file_id = message.photo[-1].file_id
        context.bot.send_photo(chat_id=GROUP_FINISH, photo=file_id, caption=caption, parse_mode=ParseMode.HTML)
        delivery_logs.append({'user': message.from_user.first_name, 'price': price, 'order': order_id})

        today = datetime.now().strftime("%Y-%m-%d")
        user_jobs[today][message.from_user.username or message.from_user.first_name].append({"job_id": order_id, "amount": price})

        log_finished_job(today, message.from_user.first_name, order_id, price, file_id)

# ==== รับงาน Callback ==== #
def button_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    user = query.from_user.first_name
    data = query.data

    if data.startswith("รับงาน_"):
        job_id = data.replace("รับงาน_", "")
        price = extract_price(query.message.text)
        context.bot.send_message(chat_id=GROUP_DROP, text=f"🚚 {user} รับงาน #ORDER{job_id} แล้ว ค่าส่ง {price}")
        query.answer("รับงานแล้ว")
        query.edit_message_reply_markup(reply_markup=None)

# ==== SUMMARY ==== #
def summary_orders(update: Update, context: CallbackContext):
    today = datetime.now().date()
    count = sum(1 for d in daily_orders if d == today)
    update.message.reply_text(f"📊 ออเดอร์วันนี้: {count} รายการ")

def summarize_jobs(context: CallbackContext, target_date: str = None):
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")
    jobs_by_user = user_jobs.get(target_date, {})
    if not jobs_by_user:
        return
    lines = [f"📅 สรุปยอดค่าส่งประจำวันที่ {target_date}\n"]
    grand_total = 0
    grand_count = 0
    for user, jobs in jobs_by_user.items():
        total = sum(j['amount'] for j in jobs)
        count = len(jobs)
        grand_total += total
        grand_count += count
        lines.append(f"👤 @{user} ({count} งาน / {total} บาท):")
        for j in jobs:
            lines.append(f" - {j['job_id']} ค่าส่ง {j['amount']}")
        lines.append("")
    lines.append(f"🧾 รวมทั้งหมด {grand_count} งาน")
    lines.append(f"💰 รวมยอดทั้งหมด = {grand_total} บาท")
    context.bot.send_message(chat_id=GROUP_FINISH, text="\n".join(lines))

    log_driver_summary(target_date, "\n".join(lines))

def summary_delivery():
    today = datetime.now().date()
    total = sum(entry['price'] for entry in delivery_logs)
    count = len(delivery_logs)
    log_daily_order(today.strftime('%Y-%m-%d'), count, total)
    message = f"📦 สรุปยอดค่าส่งประจำวันที่ {today.strftime('%d/%m/%Y')}\n"
    message += f"🚚 รวมทั้งหมด {count} ออเดอร์\n\n"
    for i, log in enumerate(delivery_logs, 1):
        message += f"{i}. {log['user']} รับงาน {log['order']} ค่าส่ง {log['price']} บาท\n"
    message += f"\n💰 รวมทั้งหมด: {total} บาท"
    return message

def daily_job(context: CallbackContext):
    summary = summary_delivery()
    context.bot.send_message(chat_id=GROUP_FINISH, text=summary)
    summarize_jobs(context)
    delivery_logs.clear()

# ==== RESET ORDER ==== #
def reset_order(update: Update, context: CallbackContext):
    global order_counter
    try:
        order_counter = 1
        save_order_counter()
        update.message.reply_text("🔁 รีเซ็ตเลขออเดอร์เป็น 1 แล้ว ✅")
    except Exception as e:
        logging.error(f"Error resetting order: {e}")

# ==== INVOICE (ตัดบิล) จากการ reply ด้วย SO- ==== #
def handle_invoice_reply(update: Update, context: CallbackContext):
    message = update.message
    if message.chat_id != GROUP_RECEIVE_ORDER:
        return

    if not message.reply_to_message:
        return

    if not message.text.startswith("SO-"):
        return

    invoice_id = message.text.strip()
    order_text = message.reply_to_message.text.strip()

    combined = f"🧾 <b>ตัดบิลสำเร็จ</b> {invoice_id}\n\n{order_text}"
    try:
        context.bot.send_message(chat_id=GROUP_BILL, text=combined, parse_mode=ParseMode.HTML)
        message.reply_text(f"✅ ส่งออเดอร์พร้อมเลขบิล {invoice_id} ไปยังกลุ่มสรุปบิลแล้ว")
    except Exception as e:
        logging.error(f"Error handling invoice reply: {e}")


# ==== CANCEL ORDER (มีการยืนยัน) ==== #
pending_cancellations = {}


def cancel_order(update: Update, context: CallbackContext):
    global order_counter
    message = update.message
    user_id = message.from_user.id

    if not message.reply_to_message:
        message.reply_text("⚠️ กรุณา reply ข้อความออเดอร์ที่ต้องการยกเลิก")
        return

    original_text = message.reply_to_message.text or message.reply_to_message.caption
    if '#ORDER' not in original_text:
        message.reply_text("⚠️ ข้อความที่ reply ไม่ใช่ออเดอร์")
        return

    match = re.search(r'#ORDER(\d+)', original_text)
    if not match:
        message.reply_text("⚠️ ไม่พบเลขออเดอร์ในข้อความ")
        return

    order_id = int(match.group(1))
    chat_id = message.chat_id
    msg_id = message.reply_to_message.message_id

    try:
        context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        logging.error(f"Error deleting message: {e}")

    # คืนสต็อก
    try:
        ws = sheet.worksheet("Stock")
        data = ws.get_all_records(expected_headers=['หมวดหมู่', 'ยี่ห้อ', 'กลิ่น', 'คงเหลือ'])
        for item in parse_order_items(original_text):
            for idx, row in enumerate(data):
                if (normalize_string(row['หมวดหมู่']) == normalize_string(item['หมวดหมู่']) and
                    normalize_string(row['ยี่ห้อ']) == normalize_string(item['ยี่ห้อ']) and
                    normalize_string(row['กลิ่น']) == normalize_string(item['กลิ่น'])):
                    current_qty = int(row['คงเหลือ'])
                    ws.update_cell(idx + 2, 4, current_qty + item['จำนวน'])
                    break
    except Exception as e:
        logging.error(f"Error restocking after cancellation: {e}")

    if order_counter == order_id + 1:
        order_counter -= 1
        save_order_counter()

    message.reply_text(f"❌ ยกเลิกออเดอร์ #ORDER{order_id} แล้ว และคืนสต็อกเรียบร้อย")


def main():
    load_order_counter()

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(MessageHandler(Filters.text & Filters.reply & Filters.regex(r"^SO-"), handle_invoice_reply))
    dp.add_handler(MessageHandler(Filters.text & Filters.chat(GROUP_RECEIVE_ORDER), handle_order))
    dp.add_handler(MessageHandler(Filters.photo & Filters.chat(GROUP_RECEIVE_ORDER), handle_order_slip))
    dp.add_handler(MessageHandler(Filters.photo & Filters.chat(GROUP_PACK), handle_pack_reply_photo))
    dp.add_handler(MessageHandler(Filters.photo & Filters.chat(GROUP_DROP), handle_drop_reply))
    dp.add_handler(CommandHandler("summary", summary_orders))
    dp.add_handler(CommandHandler("resetorder", reset_order))
    dp.add_handler(CommandHandler("cancelorder", cancel_order))
    dp.add_handler(CallbackQueryHandler(button_callback))

    job_queue = updater.job_queue
    job_queue.run_daily(daily_job, time=datetime.strptime("00:00", "%H:%M").time())

    threading.Thread(target=clean_pending_orders, daemon=True).start()

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
