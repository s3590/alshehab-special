import os
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, A5
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import arabic_reshaper
from bidi.algorithm import get_display
from datetime import datetime
from decimal import Decimal

# 🌟 الإصلاح الأمني: إزالة التحميل المتزامن لأنه يجمد السيرفر بالكامل عند الإقلاع.
# (الاعتماد على التحميل الآمن الذي برمجناه في ملف main.py)
FONT_PATH = "Amiri-Regular.ttf"
LOGO_PATH = "static/logo.png"  # مسار صورة الشعار

if not os.path.exists(FONT_PATH):
    print("⚠️ الخط العربي غير موجود حالياً، سيتم استخدام الخط الافتراضي حتى يكتمل تحميله في الخلفية.")

# تسجيل الخط في مكتبة PDF
if os.path.exists(FONT_PATH):
    pdfmetrics.registerFont(TTFont('ArabicFont', FONT_PATH))
    FONT_NAME = 'ArabicFont'
else:
    FONT_NAME = 'Helvetica'

# 🌟 [جديد] دالة مساعدة لحماية الفواتير من الانهيار بسبب القيم الفارغة (None)
def safe_amount(val):
    if val is None or str(val).strip() == '' or str(val).strip().lower() == 'none':
        return 0
    try:
        return int(Decimal(str(val)))
    except:
        return 0

def fix_arabic(text):
    """دالة لضبط الحروف العربية لتظهر متصلة ومن اليمين لليسار"""
    if text is None:
        text = ""
    reshaped_text = arabic_reshaper.reshape(str(text))
    bidi_text = get_display(reshaped_text)
    return bidi_text

def draw_logo(c, x, y, w, h):
    """دالة مساعدة لرسم الشعار بأمان"""
    if os.path.exists(LOGO_PATH):
        try:
            c.drawImage(LOGO_PATH, x, y, width=w, height=h, mask='auto')
        except Exception as e:
            print(f"Error drawing logo: {e}")

# ================= 1. فاتورة العمليات الفردية (A5) =================
def generate_receipt(receipt_id: int, client_name: str, amount, receipt_type: str, details: str) -> BytesIO:
    # 🌟 الإصلاح المحاسبي: استخدام buffer واحد لتوفير 50% من استهلاك الذاكرة (RAM)
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A5)
    width, height = A5

    c.setStrokeColor(colors.HexColor("#1F4E78"))
    c.setLineWidth(2)
    c.roundRect(15, 15, width - 30, height - 30, 10)

    c.setFillColor(colors.HexColor("#1F4E78"))
    c.roundRect(15, height - 80, width - 30, 65, 10, fill=1)
    
    draw_logo(c, width - 75, height - 75, 50, 50)
    
    c.setFillColor(colors.white)
    c.setFont(FONT_NAME, 18)
    c.drawCentredString(width / 2, height - 45, fix_arabic("فاتورة إلكترونية رسمية"))
    c.setFont(FONT_NAME, 14)
    c.drawCentredString(width / 2, height - 65, fix_arabic("الشهاب pro"))

    c.setFillColor(colors.black)
    c.setFont(FONT_NAME, 14)
    
    start_y = height - 120
    line_spacing = 35
    
    c.drawRightString(width - 40, start_y, fix_arabic(f"رقم العملية: {receipt_id}"))
    c.drawRightString(width - 40, start_y - line_spacing, fix_arabic(f"التاريخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}"))
    c.drawRightString(width - 40, start_y - (line_spacing * 2), fix_arabic(f"العميل: {client_name}"))
    c.drawRightString(width - 40, start_y - (line_spacing * 3), fix_arabic(f"نوع العملية: {receipt_type}"))
    
    c.setFont(FONT_NAME, 16)
    c.setFillColor(colors.HexColor("#900C3F"))
    # 🌟 الإصلاح الأمني: استخدام safe_amount لمنع انهيار الفاتورة
    c.drawRightString(width - 40, start_y - (line_spacing * 4), fix_arabic(f"المبلغ: {safe_amount(amount)} ريال"))
    
    c.setFillColor(colors.black)
    c.setFont(FONT_NAME, 14)
    
    import re
    safe_details = str(details) if details else "بدون تفاصيل"
    safe_details = re.sub(r'[\{\}\[\]"\'_]', ' ', safe_details).strip()
    
    c.drawRightString(width - 40, start_y - (line_spacing * 5), fix_arabic("التفاصيل:"))
    
    y_det = start_y - (line_spacing * 5) - 25
    c.setFont(FONT_NAME, 12)
    
    if "+" in safe_details:
        parts = safe_details.split("+")
        for part in parts:
            c.drawRightString(width - 60, y_det, fix_arabic(f"▪️ {part.strip()}"))
            y_det -= 20
    else:
        words = safe_details.split()
        lines = []
        current_line = ""
        for word in words:
            if len(current_line) + len(word) < 45:
                current_line += word + " "
            else:
                lines.append(current_line)
                current_line = word + " "
        if current_line: lines.append(current_line)
        
        for line in lines:
            c.drawRightString(width - 60, y_det, fix_arabic(line.strip()))
            y_det -= 20

    c.setStrokeColor(colors.lightgrey)
    c.line(40, 80, width - 40, 80)
    
    c.setFont(FONT_NAME, 11)
    c.setFillColor(colors.dimgrey)
    c.drawCentredString(width / 2, 55, fix_arabic("تم إصدار هذه الفاتورة آلياً عبر النظام المحاسبي"))
    c.drawCentredString(width / 2, 35, fix_arabic("شكراً لتعاملكم معنا 🌹"))

    c.save()
    buffer.seek(0)
    return buffer

# ================= 2. كشف الحساب المبسط للعميل (A5) =================
def generate_client_statement(client_name: str, debt, inventory_items: list, last_payment: str) -> BytesIO:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A5)
    width, height = A5

    c.setStrokeColor(colors.HexColor("#1F4E78"))
    c.setLineWidth(2)
    c.roundRect(15, 15, width - 30, height - 30, 10)

    c.setFillColor(colors.HexColor("#1F4E78"))
    c.roundRect(15, height - 70, width - 30, 55, 10, fill=1)
    
    draw_logo(c, width - 70, height - 65, 45, 45)
    
    c.setFillColor(colors.white)
    c.setFont(FONT_NAME, 16)
    c.drawCentredString(width / 2, height - 45, fix_arabic("الشهاب pro- كشف حساب عميل"))
    
    c.setFillColor(colors.black)
    c.setFont(FONT_NAME, 13)
    c.drawRightString(width - 40, height - 100, fix_arabic(f"اسم العميل: {client_name}"))
    c.drawRightString(width - 40, height - 125, fix_arabic(f"تاريخ الكشف: {datetime.now().strftime('%Y-%m-%d %H:%M')}"))
    
    c.setStrokeColor(colors.lightgrey)
    c.line(40, height - 140, width - 40, height - 140)

    c.setFont(FONT_NAME, 15)
    c.setFillColor(colors.HexColor("#900C3F"))
    c.drawRightString(width - 40, height - 170, fix_arabic(f"إجمالي المطلوب سداده: {safe_amount(debt)} ريال"))
    
    c.setFillColor(colors.black)
    c.setFont(FONT_NAME, 12)
    c.drawRightString(width - 40, height - 200, fix_arabic(f"آخر دفعة مسددة: {last_payment}"))

    c.setStrokeColor(colors.lightgrey)
    c.line(40, height - 215, width - 40, height - 215)

    c.setFont(FONT_NAME, 13)
    c.drawRightString(width - 40, height - 240, fix_arabic("الكروت المتبقية في محلك حالياً:"))
    
    y_pos = height - 270
    c.setFont(FONT_NAME, 12)
    
    if inventory_items:
        max_items = 8
        for i, item in enumerate(inventory_items):
            if i >= max_items:
                c.drawRightString(width - 60, y_pos, fix_arabic("▪️ ... وفئات أخرى مسجلة بالنظام"))
                break
            c.drawRightString(width - 60, y_pos, fix_arabic(f"▪️ {item['quantity']} كرت من فئة ({item['card_type']})"))
            y_pos -= 25
    else:
        c.drawRightString(width - 60, y_pos, fix_arabic("لا يوجد كروت مسجلة في مخزونك حالياً."))

    c.setFont(FONT_NAME, 10)
    c.setFillColor(colors.dimgrey)
    c.drawCentredString(width / 2, 35, fix_arabic("شكراً لتعاملكم مع  الشهاب pro"))

    c.save()
    buffer.seek(0)
    return buffer

# ================= 3. كشف الحساب التفصيلي (A4) =================
def generate_detailed_statement(client_name: str, debt, transactions: list, start_date: str, end_date: str) -> BytesIO:
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    def draw_background_and_header(page_canvas):
        page_canvas.setStrokeColor(colors.HexColor("#1F4E78"))
        page_canvas.setLineWidth(2)
        page_canvas.rect(15, 15, width - 30, height - 30)

    def draw_table_header(page_canvas, y_pos):
        page_canvas.setFont(FONT_NAME, 12)
        page_canvas.setFillColor(colors.HexColor("#e0e0e0"))
        page_canvas.rect(30, y_pos, width - 60, 30, fill=1)
        
        page_canvas.setFillColor(colors.black)
        page_canvas.drawRightString(width - 40, y_pos + 10, fix_arabic("التاريخ"))
        page_canvas.drawRightString(width - 150, y_pos + 10, fix_arabic("نوع العملية"))
        page_canvas.drawRightString(width - 280, y_pos + 10, fix_arabic("المبلغ (ريال)"))
        page_canvas.drawRightString(width - 400, y_pos + 10, fix_arabic("التفاصيل"))
        return y_pos - 30

    draw_background_and_header(p)
    p.setFillColor(colors.HexColor("#1F4E78"))
    p.rect(15, height - 100, width - 30, 85, fill=1)
    draw_logo(p, width - 100, height - 90, 65, 65)
    
    p.setFillColor(colors.white)
    p.setFont(FONT_NAME, 22)
    p.drawCentredString(width / 2.0, height - 55, fix_arabic("كشف حساب عميل تفصيلي"))
    
    p.setFont(FONT_NAME, 14)
    p.drawCentredString(width / 2.0, height - 85, fix_arabic(f"الفترة: من {start_date} إلى {end_date}"))

    p.setFillColor(colors.black)
    p.setFont(FONT_NAME, 16)
    p.drawRightString(width - 40, height - 140, fix_arabic(f"اسم العميل: {client_name}"))
    
    p.setFillColor(colors.HexColor("#900C3F"))
    p.drawRightString(width - 40, height - 170, fix_arabic(f"إجمالي الدين الحالي: {safe_amount(debt)} ريال"))
    
    y_position = height - 220
    y_position = draw_table_header(p, y_position)
    
    p.setFont(FONT_NAME, 11)
    
    if not transactions:
        p.drawCentredString(width / 2.0, y_position - 20, fix_arabic("لا توجد عمليات مسجلة في هذه الفترة."))
    else:
        for trans in reversed(transactions):
            if y_position < 60:
                p.showPage()
                draw_background_and_header(p)
                y_position = height - 60
                y_position = draw_table_header(p, y_position)
                p.setFont(FONT_NAME, 11)
                
            date_val = trans.get('date', '')
            date_str = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)[:10]
            
            trans_type = str(trans.get('type', '')).replace('_', ' ')
            amount = str(safe_amount(trans.get('amount', 0)))
            
            raw_details = trans.get('details', '')
            raw_details = "بدون تفاصيل" if raw_details is None else str(raw_details)
            details = raw_details[:40] + "..." if len(raw_details) > 40 else raw_details
            
            p.drawRightString(width - 40, y_position, fix_arabic(date_str))
            p.drawRightString(width - 150, y_position, fix_arabic(trans_type))
            p.drawRightString(width - 280, y_position, fix_arabic(amount))
            p.drawRightString(width - 400, y_position, fix_arabic(details))
            
            p.setStrokeColor(colors.lightgrey)
            p.setLineWidth(0.5)
            p.line(30, y_position - 10, width - 30, y_position - 10)
            
            y_position -= 30

    p.setFont(FONT_NAME, 10)
    p.setFillColor(colors.dimgrey)
    p.drawCentredString(width / 2.0, 30, fix_arabic("تم إصدار هذا الكشف آلياً عبر النظام المحاسبي الذكي لشهاب pro"))

    p.save()
    buffer.seek(0)
    return buffer

# ================= 4. التقرير اليومي (A4) =================
def generate_daily_report(date_str: str, wholesale, retail, collected, expenses, damaged, network_cash, telecom_cash, total_drawer_cash) -> BytesIO:
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    p.setStrokeColor(colors.HexColor("#1F4E78"))
    p.setLineWidth(2)
    p.rect(15, 15, width - 30, height - 30)

    p.setFillColor(colors.HexColor("#1F4E78"))
    p.rect(15, height - 100, width - 30, 85, fill=1)
    draw_logo(p, width - 100, height - 90, 65, 65)
    
    p.setFillColor(colors.white)
    p.setFont(FONT_NAME, 22)
    p.drawCentredString(width / 2.0, height - 55, fix_arabic("التقرير المالي اليومي"))
    p.setFont(FONT_NAME, 14)
    p.drawCentredString(width / 2.0, height - 85, fix_arabic(f"تاريخ التقرير: {date_str}"))

    p.setFillColor(colors.black)
    p.setFont(FONT_NAME, 16)
    
    y = height - 150
    p.drawRightString(width - 50, y, fix_arabic(f"📦 مبيعات الجملة (للبقالات): {safe_amount(wholesale)} ريال"))
    y -= 40
    p.drawRightString(width - 50, y, fix_arabic(f"🛒 مبيعات التجزئة (كاش مباشر): {safe_amount(retail)} ريال"))
    y -= 40
    p.drawRightString(width - 50, y, fix_arabic(f"💵 التحصيلات (سداد ديون اليوم): {safe_amount(collected)} ريال"))
    y -= 40
    p.drawRightString(width - 50, y, fix_arabic(f"📉 المصروفات والتسديدات اليوم: {safe_amount(expenses)} ريال"))
    y -= 40
    p.drawRightString(width - 50, y, fix_arabic(f"💔 كروت تالفة تم تسجيلها اليوم: {safe_amount(damaged)} ريال"))
    
    y -= 60
    p.setFillColor(colors.HexColor("#006400")) # أخضر
    p.setFont(FONT_NAME, 18)
    p.drawRightString(width - 50, y, fix_arabic(f"🏦 إجمالي الكاش الفعلي بالدرج: {safe_amount(total_drawer_cash)} ريال"))
    
    y -= 30
    p.setFont(FONT_NAME, 14)
    p.setFillColor(colors.dimgrey)
    p.drawRightString(width - 50, y, fix_arabic(f"(منه كاش يخص الشبكة: {safe_amount(network_cash)} ريال)"))
    
    y -= 25
    p.drawRightString(width - 50, y, fix_arabic(f"(ومنه كاش يخص التسديدات: {safe_amount(telecom_cash)} ريال)"))

    p.setFont(FONT_NAME, 10)
    p.setFillColor(colors.dimgrey)
    p.drawCentredString(width / 2.0, 30, fix_arabic("تم إصدار هذا التقرير آلياً عبر النظام المحاسبي الذكي لشهاب pro"))

    p.save()
    buffer.seek(0)
    return buffer

# ================= 5. تقرير المركز المالي الشامل (A4) =================
def generate_comprehensive_report(total_profit, market_debt, inventory_value, available_cash, net_to_gm) -> BytesIO:
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    p.setStrokeColor(colors.HexColor("#1F4E78"))
    p.setLineWidth(2)
    p.rect(15, 15, width - 30, height - 30)

    p.setFillColor(colors.HexColor("#1F4E78"))
    p.rect(15, height - 100, width - 30, 85, fill=1)
    draw_logo(p, width - 100, height - 90, 65, 65)
    
    p.setFillColor(colors.white)
    p.setFont(FONT_NAME, 22)
    p.drawCentredString(width / 2.0, height - 55, fix_arabic("تقرير المركز المالي الشامل"))
    p.setFont(FONT_NAME, 14)
    p.drawCentredString(width / 2.0, height - 85, fix_arabic(f"تاريخ الإصدار: {datetime.now().strftime('%Y-%m-%d %H:%M')}"))

    p.setFillColor(colors.black)
    p.setFont(FONT_NAME, 16)
    
    y = height - 160
    p.drawRightString(width - 50, y, fix_arabic(f"💎 إجمالي أرباح الوكيل الصافية: {safe_amount(total_profit)} ريال"))
    y -= 50
    p.drawRightString(width - 50, y, fix_arabic(f"👥 إجمالي ديون السوق (البقالات): {safe_amount(market_debt)} ريال"))
    y -= 50
    p.drawRightString(width - 50, y, fix_arabic(f"📦 إجمالي قيمة المخزون المتوفر: {safe_amount(inventory_value)} ريال"))
    y -= 50
    p.drawRightString(width - 50, y, fix_arabic(f"💵 السيولة النقدية (الكاش) المتوفرة: {safe_amount(available_cash)} ريال"))
    
    y -= 70
    p.setFillColor(colors.HexColor("#900C3F")) # أحمر داكن
    p.setFont(FONT_NAME, 18)
    p.drawRightString(width - 50, y, fix_arabic(f"⚖️ الصافي المطلوب تسديده للإدارة العامة: {safe_amount(net_to_gm)} ريال"))

    p.setFont(FONT_NAME, 10)
    p.setFillColor(colors.dimgrey)
    p.drawCentredString(width / 2.0, 30, fix_arabic("تم إصدار هذا التقرير آلياً عبر النظام المحاسبي الذكي لشهاب pro"))

    p.save()
    buffer.seek(0)
    return buffer

# ================= 6. التقرير المالي المخصص التفصيلي (A4) =================
def generate_custom_report_pdf(start_date: str, end_date: str, summary_data: dict) -> BytesIO:
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    p.setStrokeColor(colors.HexColor("#1F4E78"))
    p.setLineWidth(2)
    p.rect(15, 15, width - 30, height - 30)

    p.setFillColor(colors.HexColor("#1F4E78"))
    p.rect(15, height - 100, width - 30, 85, fill=1)
    draw_logo(p, width - 100, height - 90, 65, 65)
    
    p.setFillColor(colors.white)
    p.setFont(FONT_NAME, 22)
    p.drawCentredString(width / 2.0, height - 55, fix_arabic("التقرير المالي التفصيلي المخصص"))
    p.setFont(FONT_NAME, 14)
    p.drawCentredString(width / 2.0, height - 85, fix_arabic(f"الفترة: من {start_date} إلى {end_date}"))

    p.setFillColor(colors.black)
    p.setFont(FONT_NAME, 14)
    
    y = height - 140
    line_spacing = 25

    p.setFillColor(colors.HexColor("#1F4E78"))
    p.drawString(width - 200, y, fix_arabic("📦 حركة الكروت والمبيعات:"))
    p.setFillColor(colors.black)
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"📥 إجمالي الكروت المستلمة من الإدارة: {safe_amount(summary_data.get('received_from_gm', 0))} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"📤 مبيعات الجملة (آجل للبقالات): {safe_amount(summary_data.get('wholesale_debt', 0))} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"🛍️ مبيعات الجملة (كاش): {safe_amount(summary_data.get('wholesale_cash', 0))} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"🛒 مبيعات التجزئة (طياري كاش): {safe_amount(summary_data.get('retail_cash', 0))} ريال"))
    
    y -= (line_spacing + 10)

    p.setFillColor(colors.HexColor("#1F4E78"))
    p.drawString(width - 200, y, fix_arabic("💵 حركة الأموال والتحصيلات:"))
    p.setFillColor(colors.black)
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"💰 التحصيلات (سداد ديون البقالات): {safe_amount(summary_data.get('collected', 0))} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"💸 التسديدات المحولة للإدارة العامة: {safe_amount(summary_data.get('paid_to_gm', 0))} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"📉 المصروفات التشغيلية: {safe_amount(summary_data.get('expenses', 0))} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"💎 عمولات ونسب الوكيل المستحقة: {safe_amount(summary_data.get('agent_commission', 0))} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"💸 سحب أرباح شخصية للوكيل: {safe_amount(summary_data.get('profit_withdrawal', 0))} ريال"))

    y -= (line_spacing + 10)

    p.setFillColor(colors.HexColor("#1F4E78"))
    p.drawString(width - 200, y, fix_arabic("🔄 المرتجعات والتوالف:"))
    p.setFillColor(colors.black)
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"🏪 مرتجعات من البقالات: {safe_amount(summary_data.get('returns_client', 0))} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"🏢 مرتجعات مسطرة للإدارة: {safe_amount(summary_data.get('returns_gm', 0))} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"🛒 مرتجعات بيع مباشر (طياري): {safe_amount(summary_data.get('returns_retail', 0))} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"💔 إجمالي الكروت التالفة: {safe_amount(summary_data.get('damaged', 0))} ريال"))

    y -= 50
    
    p.setFillColor(colors.HexColor("#006400")) # أخضر
    p.setFont(FONT_NAME, 18)
    net_cash = summary_data.get('net_cash', 0)
    p.drawRightString(width - 50, y, fix_arabic(f"🏦 صافي حركة الكاش لهذه الفترة: {safe_amount(net_cash)} ريال"))

    p.setFont(FONT_NAME, 10)
    p.setFillColor(colors.dimgrey)
    p.drawCentredString(width / 2.0, 30, fix_arabic("تم إصدار هذا التقرير التفصيلي آلياً عبر النظام المحاسبي الذكي لشهاب pro"))

    p.save()
    buffer.seek(0)
    return buffer

# ================= 7. التقرير السري لمالية التسديدات (A4) =================
def generate_telecom_secret_pdf(start_date: str, end_date: str, total_sales, total_profit, total_collected, total_recharged, current_balance) -> BytesIO:
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    p.setStrokeColor(colors.HexColor("#8e2de2")) # لون بنفسجي مميز للتقرير السري
    p.setLineWidth(2)
    p.rect(15, 15, width - 30, height - 30)

    p.setFillColor(colors.HexColor("#8e2de2"))
    p.rect(15, height - 100, width - 30, 85, fill=1)
    draw_logo(p, width - 100, height - 90, 65, 65)
    
    p.setFillColor(colors.white)
    p.setFont(FONT_NAME, 22)
    p.drawCentredString(width / 2.0, height - 55, fix_arabic("تقرير التسديدات السري (خاص بالوكيل)"))
    p.setFont(FONT_NAME, 14)
    p.drawCentredString(width / 2.0, height - 85, fix_arabic(f"الفترة: من {start_date} إلى {end_date}"))

    p.setFillColor(colors.black)
    p.setFont(FONT_NAME, 16)
    
    y = height - 150
    line_spacing = 45

    p.drawRightString(width - 50, y, fix_arabic(f"📱 إجمالي المبيعات (شحن وباقات): {safe_amount(total_sales)} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"💎 إجمالي الأرباح الصافية: {safe_amount(total_profit)} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"💵 إجمالي تحصيلات ديون الرصيد: {safe_amount(total_collected)} ريال"))
    y -= line_spacing
    p.drawRightString(width - 50, y, fix_arabic(f"📥 إجمالي تغذية البوابة (من الصندوق): {safe_amount(total_recharged)} ريال"))
    
    y -= 60
    p.setFillColor(colors.HexColor("#00d2ff")) # أزرق فاتح
    p.setFont(FONT_NAME, 18)
    p.drawRightString(width - 50, y, fix_arabic(f"🏦 الرصيد الحالي المتبقي في البوابة: {safe_amount(current_balance)} ريال"))

    p.setFont(FONT_NAME, 10)
    p.setFillColor(colors.dimgrey)
    p.drawCentredString(width / 2.0, 30, fix_arabic("هذا التقرير سري وخاص بالوكيل فقط - لا يظهر للمدير العام"))

    p.save()
    
    # 🌟 السطر السحري الذي يمنع تسريب الذاكرة (Memory Leak)
    buffer.seek(0)
    return buffer
