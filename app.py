import os
import json
import sqlite3
import hashlib
import io
import logging
import pandas as pd
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, send_from_directory
from werkzeug.utils import secure_filename
from paddleocr import PaddleOCR
from pdf2image import convert_from_path
import re
from PIL import Image
from functools import wraps
from contextlib import contextmanager

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='')
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['ALLOWED_EXTENSIONS'] = {'pdf'}
app.config['PRELOAD_OCR'] = os.environ.get('PRELOAD_OCR', 'false').lower() == 'true'

# Initialize PaddleOCR
ocr = None

def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def validate_invoice_type(invoice_type):
    """验证发票类型"""
    valid_types = {'income', 'expense', 'other'}  # 根据实际需求调整
    return invoice_type in valid_types if valid_types else True

def init_ocr():
    """初始化 OCR 引擎"""
    global ocr
    if ocr is None:
        logger.info('正在初始化 PaddleOCR...')
        ocr = PaddleOCR(use_textline_orientation=True, lang='ch')
        logger.info('PaddleOCR 初始化完成')
    return ocr

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Database context manager
@contextmanager
def get_db_connection():
    """数据库连接上下文管理器，确保连接正确关闭"""
    conn = sqlite3.connect('invoices.db')
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# Database initialization
def init_db():
    conn = sqlite3.connect('invoices.db')
    cursor = conn.cursor()
    
    # 创建主表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            buyer_name TEXT NOT NULL,
            invoice_number TEXT,
            invoice_date TEXT,
            total_amount TEXT,
            invoice_content TEXT,
            seller_name TEXT,
            bank_name TEXT,
            bank_account TEXT,
            pdf_path TEXT,
            file_hash TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    ''')
    
    # 创建回收站表 (修复：添加 updated_at)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS recycle_bin (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            buyer_name TEXT NOT NULL,
            invoice_number TEXT,
            invoice_date TEXT,
            total_amount TEXT,
            invoice_content TEXT,
            seller_name TEXT,
            bank_name TEXT,
            bank_account TEXT,
            pdf_path TEXT,
            file_hash TEXT,
            created_at TEXT,
            updated_at TEXT,
            deleted_at TEXT
        )
    ''')
    
    # 自动修复补丁 - 添加可能缺失的列
    try: cursor.execute('ALTER TABLE invoices ADD COLUMN file_hash TEXT')
    except sqlite3.OperationalError: pass
    try: cursor.execute('ALTER TABLE invoices ADD COLUMN created_at TEXT')
    except sqlite3.OperationalError: pass
    try: cursor.execute('ALTER TABLE invoices ADD COLUMN updated_at TEXT')
    except sqlite3.OperationalError: pass
    
    try: cursor.execute('ALTER TABLE recycle_bin ADD COLUMN file_hash TEXT')
    except sqlite3.OperationalError: pass
    try: cursor.execute('ALTER TABLE recycle_bin ADD COLUMN created_at TEXT')
    except sqlite3.OperationalError: pass
    # 核心修复：给回收站补上 updated_at
    try: cursor.execute('ALTER TABLE recycle_bin ADD COLUMN updated_at TEXT')
    except sqlite3.OperationalError: pass
    
    # 创建索引以提高查询性能
    try: cursor.execute('CREATE INDEX IF NOT EXISTS idx_invoices_type ON invoices(type)')
    except sqlite3.OperationalError: pass
    try: cursor.execute('CREATE INDEX IF NOT EXISTS idx_invoices_invoice_number ON invoices(invoice_number)')
    except sqlite3.OperationalError: pass
    try: cursor.execute('CREATE INDEX IF NOT EXISTS idx_invoices_file_hash ON invoices(file_hash)')
    except sqlite3.OperationalError: pass
    try: cursor.execute('CREATE INDEX IF NOT EXISTS idx_recycle_bin_type ON recycle_bin(type)')
    except sqlite3.OperationalError: pass
    
    conn.commit()
    conn.close()
    logger.info('数据库初始化完成')

init_db()

# 根据配置预加载 OCR
if app.config['PRELOAD_OCR']:
    init_ocr()

def get_file_hash(file_stream):
    """计算文件的 MD5 哈希值"""
    md5_hash = hashlib.md5()
    for byte_block in iter(lambda: file_stream.read(4096), b""):
        md5_hash.update(byte_block)
    file_stream.seek(0)
    return md5_hash.hexdigest()

def extract_invoice_info(image_path):
    """Extract invoice information using OCR"""
    ocr_engine = init_ocr()
    
    result = ocr_engine.ocr(image_path, cls=True)
    
    all_text = []
    if result and result[0]:
        for line in result[0]:
            all_text.append(line[1][0])
    
    full_text = ' '.join(all_text)
    
    invoice_data = {
        'invoice_number': '', 'invoice_date': '', 'total_amount': '', 
        'invoice_content': '', 'seller_name': '', 'bank_name': '', 'bank_account': ''
    }
    
    # --- 1. 发票号码 (8-20位) ---
    invoice_num_match = re.search(r'发票号码.*?(\d{8,20})', full_text)
    if invoice_num_match:
        invoice_data['invoice_number'] = invoice_num_match.group(1)
    
    # --- 2. 开票日期 ---
    date_patterns = [
        r'(\d{4}年\d{1,2}月\d{1,2}日)', r'(\d{8})',
        r'开票日期.*?(\d{4}年\d{1,2}月\d{1,2}日)', r'开票日期.*?(\d{8})'
    ]
    for pattern in date_patterns:
        date_match = re.search(pattern, full_text)
        if date_match:
            date_str = date_match.group(1).replace('年', '').replace('月', '').replace('日', '')
            if len(date_str) == 7:
                parts = re.findall(r'\d+', date_match.group(1))
                if len(parts) == 3:
                    date_str = f"{parts[0]}{parts[1].zfill(2)}{parts[2].zfill(2)}"
            invoice_data['invoice_date'] = date_str
            break
    
    # --- 3. 总金额 ---
    amount_patterns = [
        r'价税合计.*?¥\s*([\d,]+\.?\d*)', r'价税合计.*?([\d,]+\.?\d*)',
        r'合计.*?¥\s*([\d,]+\.?\d*)', r'总金额.*?¥\s*([\d,]+\.?\d*)'
    ]
    for pattern in amount_patterns:
        amount_match = re.search(pattern, full_text)
        if amount_match:
            invoice_data['total_amount'] = amount_match.group(1).replace(',', '')
            break
    
    # --- 4. 发票内容 (修复：只取*号中间的内容及紧跟的文字) ---
    # 逻辑：寻找 *xxx*yyyy 格式，或者 "名称" 后面紧跟的非表头文字
    content_patterns = [
        r'(\*[\u4e00-\u9fa5]+\*[^\s\d¥*]+)', # 优先匹配 *分类*商品名，例如 *生物化学制品*试剂
        r'(\*[\u4e00-\u9fa5]+\*)',         # 如果没有商品名，至少匹配 *生物化学制品*
        r'货物或应税劳务名称.*?[:：]?\s*([^\d¥\s]+)(?=\s)', 
        r'项目名称.*?[:：]?\s*([^\d¥\s]+)(?=\s)'
    ]
    for pattern in content_patterns:
        content_match = re.search(pattern, full_text)
        if content_match:
            content = content_match.group(1).strip()
            # 再次检查，如果抓到了“规格”、“单位”等表头，说明抓错了，跳过
            if "规格" not in content and "单价" not in content and "单位" not in content and "数量" not in content:
                invoice_data['invoice_content'] = content
                break
    
    # --- 5. 销售方名称 (修复：更严格的截止词) ---
    # 逻辑：先定位到“销售方”，然后找“名称”，然后抓取公司名，遇到“买方”、“统一”、“纳税人”等立刻停止
    seller_patterns = [
        # 尝试匹配：销售方...名称：xxx公司
        r'销售方.*?名称[:：]?\s*([\u4e00-\u9fa5]+(?:公司|中心|厂|店|行))',
        # 尝试匹配：销售方...名称：xxx (直到遇到干扰词)
        r'销售方.*?名称[:：]?\s*([^\s]+)(?=\s+(?:买方|名称|纳税人|统一|地址|电话|注|开户|银行)|$)'
    ]
    for pattern in seller_patterns:
        seller_match = re.search(pattern, full_text)
        if seller_match:
            name = seller_match.group(1).strip()
            # 清洗前面的干扰词 (比如 OCR 把 "信" 字也识别进来了)
            name = re.sub(r'^[^\u4e00-\u9fa5]+', '', name) # 去掉开头非汉字字符
            if len(name) > 4: # 公司名通常大于4个字
                invoice_data['seller_name'] = name
                break
            
    # --- 6. 银行信息 (修复：遇到分号或“账号”立即停止) ---
    bank_patterns = [
        # 匹配 开户行... 之后的文字，直到遇到 ; ； 账号 银行账号 或行尾
        r'开户(?:银行|行)[:：]?\s*([^\s;；]+)(?=\s*[;；]|\s+(?:银行)?账号|$)',
    ]
    for pattern in bank_patterns:
        bank_match = re.search(pattern, full_text)
        if bank_match:
            bank = bank_match.group(1).strip()
            # 清理可能残留的尾部标点
            bank = re.sub(r'[;；:：]+$', '', bank)
            invoice_data['bank_name'] = bank
            break
            
    # --- 7. 银行账号 ---
    account_patterns = [r'(?:银行)?账号[:：]?\s*(\d{10,30})']
    for pattern in account_patterns:
        account_match = re.search(pattern, full_text)
        if account_match:
            invoice_data['bank_account'] = account_match.group(1).strip()
            break
    
    return invoice_data

@app.route('/')
def index():
    return send_file('static/index.html')

@app.route('/api/upload', methods=['POST'])
def upload_invoice():
    filepath = None
    img_path = None
    
    try:
        if 'file' not in request.files:
            return jsonify({'error': '没有上传文件'}), 400
        
        file = request.files['file']
        invoice_type = request.form.get('type')
        buyer_name = request.form.get('buyer_name', '').strip()
        force_upload = request.form.get('force') == 'true'
        
        if file.filename == '':
            return jsonify({'error': '没有选择文件'}), 400
        
        if not (file and file.filename.lower().endswith('.pdf')):
            return jsonify({'error': '请上传PDF文件'}), 400
        
        file_hash = get_file_hash(file.stream)
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            if not force_upload:
                cursor.execute('SELECT id FROM invoices WHERE file_hash = ?', (file_hash,))
                if cursor.fetchone():
                    return jsonify({
                        'warning': 'duplicate_file',
                        'message': '该发票文件已经上传过，是否继续上传？'
                    }), 409

            filename = secure_filename(file.filename)
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            filename = f"{timestamp}_{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            images = convert_from_path(filepath, first_page=1, last_page=1)
            if not images:
                raise ValueError('PDF 转换图片失败')
            
            img_path = filepath.replace('.pdf', '.jpg')
            images[0].save(img_path, 'JPEG')
            
            invoice_data = extract_invoice_info(img_path)
            os.remove(img_path)
            img_path = None
            
            if not force_upload and invoice_data['invoice_number']:
                cursor.execute('SELECT id FROM invoices WHERE invoice_number = ?', (invoice_data['invoice_number'],))
                if cursor.fetchone():
                    os.remove(filepath)
                    filepath = None
                    return jsonify({
                        'warning': 'duplicate_number',
                        'message': f"发票号码 {invoice_data['invoice_number']} 已经存在，是否继续上传？"
                    }), 409
            
            china_time = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')

            cursor.execute('''
                INSERT INTO invoices (type, buyer_name, invoice_number, invoice_date, 
                                    total_amount, invoice_content, seller_name, 
                                    bank_name, bank_account, pdf_path, file_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (invoice_type, buyer_name, invoice_data['invoice_number'],
                  invoice_data['invoice_date'], invoice_data['total_amount'],
                  invoice_data['invoice_content'], invoice_data['seller_name'],
                  invoice_data['bank_name'], invoice_data['bank_account'], 
                  filename, file_hash, china_time, china_time))
            conn.commit()
            
            return jsonify({
                'success': True,
                'message': '发票上传成功',
                'data': invoice_data
            })
    
    except Exception as e:
        # 清理已保存的文件
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
        if img_path and os.path.exists(img_path):
            os.remove(img_path)
        logger.error(f'上传失败: {str(e)}')
        return jsonify({'error': f'上传失败: {str(e)}'}), 500

@app.route('/api/invoices/<invoice_type>', methods=['GET'])
def get_invoices(invoice_type):
    try:
        sort_by = request.args.get('sort_by', 'created_at')
        order = request.args.get('order', 'DESC')
        
        # 白名单验证排序字段，防止 SQL 注入
        valid_sorts = {
            'invoice_date': 'invoice_date',
            'created_at': 'created_at',
            'invoice_number': 'invoice_number',
            'buyer_name': 'buyer_name',
            'total_amount': 'CAST(total_amount AS REAL)'
        }
        sort_field = valid_sorts.get(sort_by, 'created_at')
        sort_order = 'ASC' if order.upper() == 'ASC' else 'DESC'
        
        # 使用预定义的安全 SQL 模板
        queries = {
            ('invoice_date', 'ASC'): 'SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount, invoice_content, seller_name, bank_name, bank_account, pdf_path, created_at FROM invoices WHERE type = ? ORDER BY invoice_date ASC',
            ('invoice_date', 'DESC'): 'SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount, invoice_content, seller_name, bank_name, bank_account, pdf_path, created_at FROM invoices WHERE type = ? ORDER BY invoice_date DESC',
            ('created_at', 'ASC'): 'SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount, invoice_content, seller_name, bank_name, bank_account, pdf_path, created_at FROM invoices WHERE type = ? ORDER BY created_at ASC',
            ('created_at', 'DESC'): 'SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount, invoice_content, seller_name, bank_name, bank_account, pdf_path, created_at FROM invoices WHERE type = ? ORDER BY created_at DESC',
            ('invoice_number', 'ASC'): 'SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount, invoice_content, seller_name, bank_name, bank_account, pdf_path, created_at FROM invoices WHERE type = ? ORDER BY invoice_number ASC',
            ('invoice_number', 'DESC'): 'SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount, invoice_content, seller_name, bank_name, bank_account, pdf_path, created_at FROM invoices WHERE type = ? ORDER BY invoice_number DESC',
            ('buyer_name', 'ASC'): 'SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount, invoice_content, seller_name, bank_name, bank_account, pdf_path, created_at FROM invoices WHERE type = ? ORDER BY buyer_name ASC',
            ('buyer_name', 'DESC'): 'SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount, invoice_content, seller_name, bank_name, bank_account, pdf_path, created_at FROM invoices WHERE type = ? ORDER BY buyer_name DESC',
            ('total_amount', 'ASC'): 'SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount, invoice_content, seller_name, bank_name, bank_account, pdf_path, created_at FROM invoices WHERE type = ? ORDER BY CAST(total_amount AS REAL) ASC',
            ('total_amount', 'DESC'): 'SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount, invoice_content, seller_name, bank_name, bank_account, pdf_path, created_at FROM invoices WHERE type = ? ORDER BY CAST(total_amount AS REAL) DESC',
        }
        
        query = queries.get((sort_by if sort_by in valid_sorts else 'created_at', sort_order),
                           queries[('created_at', 'DESC')])
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (invoice_type,))
            rows = cursor.fetchall()
            invoices = [dict(row) for row in rows]
        
        return jsonify({'success': True, 'data': invoices})
    
    except Exception as e:
        return jsonify({'error': f'查询失败: {str(e)}'}), 500

@app.route('/api/export/<invoice_type>', methods=['GET'])
def export_invoices(invoice_type):
    try:
        with get_db_connection() as conn:
            df = pd.read_sql_query("SELECT * FROM invoices WHERE type = ?", conn, params=(invoice_type,))
        
        if df.empty:
             return jsonify({'error': '没有数据可导出'}), 400

        rename_map = {
            'type': '类型', 'buyer_name': '购买人', 'invoice_number': '发票号码',
            'invoice_date': '开票日期', 'total_amount': '总金额', 'invoice_content': '内容',
            'seller_name': '销售方', 'bank_name': '开户行', 'bank_account': '账号',
            'file_hash': '文件哈希', 'created_at': '上传时间'
        }
        df = df.rename(columns=rename_map)
        
        cols_to_drop = ['id', 'pdf_path', '文件哈希']
        df = df.drop(columns=[c for c in cols_to_drop if c in df.columns])

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Invoices')
        output.seek(0)
        
        filename = f"{invoice_type}_invoices_{datetime.now().strftime('%Y%m%d')}.xlsx"
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        return jsonify({'error': f'导出失败: {str(e)}'}), 500

@app.route('/api/invoices/delete', methods=['POST'])
def delete_invoices():
    try:
        data = request.json
        invoice_ids = data.get('ids', [])
        
        if not invoice_ids:
            return jsonify({'error': '没有选择要删除的发票'}), 400
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            for invoice_id in invoice_ids:
                invoice = cursor.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
                
                if invoice:
                    row_dict = dict(invoice)
                    if 'id' in row_dict: del row_dict['id']
                    
                    keys = list(row_dict.keys())
                    values = list(row_dict.values())
                    
                    keys.append('deleted_at')
                    china_time = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
                    values.append(china_time)
                    
                    placeholders = ', '.join(['?'] * len(keys))
                    columns = ', '.join(keys)
                    
                    cursor.execute(f'INSERT INTO recycle_bin ({columns}) VALUES ({placeholders})', values)
                    cursor.execute('DELETE FROM invoices WHERE id = ?', (invoice_id,))
            
            conn.commit()
        
        return jsonify({'success': True, 'message': '删除成功'})
    
    except Exception as e:
        return jsonify({'error': f'删除失败: {str(e)}'}), 500

@app.route('/api/recycle-bin/<invoice_type>', methods=['GET'])
def get_recycle_bin(invoice_type):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            thirty_days_ago = (datetime.utcnow() + timedelta(hours=8) - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('DELETE FROM recycle_bin WHERE deleted_at < ?', (thirty_days_ago,))
            
            cursor.execute('''
                SELECT *
                FROM recycle_bin
                WHERE type = ?
                ORDER BY deleted_at DESC
            ''', (invoice_type,))
            
            rows = cursor.fetchall()
            invoices = [dict(row) for row in rows]
            conn.commit()
        
        return jsonify({'success': True, 'data': invoices})
    
    except Exception as e:
        return jsonify({'error': f'查询失败: {str(e)}'}), 500

@app.route('/api/recycle-bin/restore', methods=['POST'])
def restore_invoices():
    """从回收站恢复发票"""
    try:
        data = request.json
        invoice_ids = data.get('ids', [])
        
        if not invoice_ids:
            return jsonify({'error': '没有选择要恢复的发票'}), 400
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            for invoice_id in invoice_ids:
                invoice = cursor.execute('SELECT * FROM recycle_bin WHERE id = ?', (invoice_id,)).fetchone()
                
                if invoice:
                    row_dict = dict(invoice)
                    # 移除回收站特有的字段
                    if 'id' in row_dict: del row_dict['id']
                    if 'deleted_at' in row_dict: del row_dict['deleted_at']
                    
                    keys = list(row_dict.keys())
                    values = list(row_dict.values())
                    
                    placeholders = ', '.join(['?'] * len(keys))
                    columns = ', '.join(keys)
                    
                    cursor.execute(f'INSERT INTO invoices ({columns}) VALUES ({placeholders})', values)
                    cursor.execute('DELETE FROM recycle_bin WHERE id = ?', (invoice_id,))
            
            conn.commit()
        
        return jsonify({'success': True, 'message': '恢复成功'})
    
    except Exception as e:
        return jsonify({'error': f'恢复失败: {str(e)}'}), 500

@app.route('/api/recycle-bin/permanent-delete', methods=['POST'])
def permanent_delete_invoices():
    """永久删除回收站中的发票"""
    try:
        data = request.json
        invoice_ids = data.get('ids', [])
        
        if not invoice_ids:
            return jsonify({'error': '没有选择要删除的发票'}), 400
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            for invoice_id in invoice_ids:
                # 获取 PDF 路径以便删除文件
                invoice = cursor.execute('SELECT pdf_path FROM recycle_bin WHERE id = ?', (invoice_id,)).fetchone()
                
                if invoice and invoice['pdf_path']:
                    pdf_file = os.path.join(app.config['UPLOAD_FOLDER'], invoice['pdf_path'])
                    if os.path.exists(pdf_file):
                        os.remove(pdf_file)
                
                cursor.execute('DELETE FROM recycle_bin WHERE id = ?', (invoice_id,))
            
            conn.commit()
        
        return jsonify({'success': True, 'message': '永久删除成功'})
    
    except Exception as e:
        return jsonify({'error': f'删除失败: {str(e)}'}), 500

@app.route('/api/recycle-bin/empty', methods=['POST'])
def empty_recycle_bin():
    """清空回收站"""
    try:
        invoice_type = request.json.get('type') if request.json else None
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            if invoice_type:
                # 获取所有需要删除的 PDF 文件
                cursor.execute('SELECT pdf_path FROM recycle_bin WHERE type = ?', (invoice_type,))
                rows = cursor.fetchall()
                
                for row in rows:
                    if row['pdf_path']:
                        pdf_file = os.path.join(app.config['UPLOAD_FOLDER'], row['pdf_path'])
                        if os.path.exists(pdf_file):
                            os.remove(pdf_file)
                
                cursor.execute('DELETE FROM recycle_bin WHERE type = ?', (invoice_type,))
            else:
                # 清空所有
                cursor.execute('SELECT pdf_path FROM recycle_bin')
                rows = cursor.fetchall()
                
                for row in rows:
                    if row['pdf_path']:
                        pdf_file = os.path.join(app.config['UPLOAD_FOLDER'], row['pdf_path'])
                        if os.path.exists(pdf_file):
                            os.remove(pdf_file)
                
                cursor.execute('DELETE FROM recycle_bin')
            
            conn.commit()
        
        return jsonify({'success': True, 'message': '回收站已清空'})
    
    except Exception as e:
        return jsonify({'error': f'清空失败: {str(e)}'}), 500

@app.route('/api/invoices/<int:invoice_id>', methods=['PUT'])
def update_invoice(invoice_id):
    """更新发票信息"""
    try:
        data = request.json
        if not data:
            return jsonify({'error': '没有提供更新数据'}), 400
        
        # 允许更新的字段白名单
        allowed_fields = {
            'buyer_name', 'invoice_number', 'invoice_date', 'total_amount',
            'invoice_content', 'seller_name', 'bank_name', 'bank_account', 'type'
        }
        
        # 过滤出允许更新的字段
        update_data = {k: v for k, v in data.items() if k in allowed_fields}
        
        if not update_data:
            return jsonify({'error': '没有有效的更新字段'}), 400
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # 检查发票是否存在
            cursor.execute('SELECT id FROM invoices WHERE id = ?', (invoice_id,))
            if not cursor.fetchone():
                return jsonify({'error': '发票不存在'}), 404
            
            # 构建更新语句
            update_data['updated_at'] = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
            
            set_clause = ', '.join([f'{k} = ?' for k in update_data.keys()])
            values = list(update_data.values()) + [invoice_id]
            
            cursor.execute(f'UPDATE invoices SET {set_clause} WHERE id = ?', values)
            conn.commit()
            
            logger.info(f'发票 {invoice_id} 已更新')
        
        return jsonify({'success': True, 'message': '更新成功'})
    
    except Exception as e:
        logger.error(f'更新发票失败: {str(e)}')
        return jsonify({'error': f'更新失败: {str(e)}'}), 500

@app.route('/api/invoices/<int:invoice_id>', methods=['GET'])
def get_invoice_detail(invoice_id):
    """获取单个发票详情"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,))
            row = cursor.fetchone()
            
            if not row:
                return jsonify({'error': '发票不存在'}), 404
            
            return jsonify({'success': True, 'data': dict(row)})
    
    except Exception as e:
        return jsonify({'error': f'查询失败: {str(e)}'}), 500

@app.route('/api/search', methods=['GET'])
def search_invoices():
    """搜索发票"""
    try:
        keyword = request.args.get('keyword', '').strip()
        invoice_type = request.args.get('type', '')
        start_date = request.args.get('start_date', '')
        end_date = request.args.get('end_date', '')
        min_amount = request.args.get('min_amount', '')
        max_amount = request.args.get('max_amount', '')
        
        conditions = []
        params = []
        
        if keyword:
            conditions.append('''
                (invoice_number LIKE ? OR buyer_name LIKE ? OR 
                 seller_name LIKE ? OR invoice_content LIKE ?)
            ''')
            keyword_param = f'%{keyword}%'
            params.extend([keyword_param] * 4)
        
        if invoice_type:
            conditions.append('type = ?')
            params.append(invoice_type)
        
        if start_date:
            conditions.append('invoice_date >= ?')
            params.append(start_date.replace('-', ''))
        
        if end_date:
            conditions.append('invoice_date <= ?')
            params.append(end_date.replace('-', ''))
        
        if min_amount:
            conditions.append('CAST(total_amount AS REAL) >= ?')
            params.append(float(min_amount))
        
        if max_amount:
            conditions.append('CAST(total_amount AS REAL) <= ?')
            params.append(float(max_amount))
        
        where_clause = ' AND '.join(conditions) if conditions else '1=1'
        
        query = f'''
            SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount,
                   invoice_content, seller_name, bank_name, bank_account, pdf_path, created_at
            FROM invoices
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT 100
        '''
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            invoices = [dict(row) for row in rows]
        
        return jsonify({'success': True, 'data': invoices, 'count': len(invoices)})
    
    except Exception as e:
        logger.error(f'搜索失败: {str(e)}')
        return jsonify({'error': f'搜索失败: {str(e)}'}), 500

@app.route('/api/statistics', methods=['GET'])
def get_statistics():
    """获取发票统计信息"""
    try:
        invoice_type = request.args.get('type', '')
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # 基础统计
            if invoice_type:
                cursor.execute('''
                    SELECT 
                        COUNT(*) as total_count,
                        COALESCE(SUM(CAST(total_amount AS REAL)), 0) as total_amount,
                        COALESCE(AVG(CAST(total_amount AS REAL)), 0) as avg_amount,
                        COALESCE(MAX(CAST(total_amount AS REAL)), 0) as max_amount,
                        COALESCE(MIN(CAST(total_amount AS REAL)), 0) as min_amount
                    FROM invoices
                    WHERE type = ? AND total_amount != ''
                ''', (invoice_type,))
            else:
                cursor.execute('''
                    SELECT 
                        COUNT(*) as total_count,
                        COALESCE(SUM(CAST(total_amount AS REAL)), 0) as total_amount,
                        COALESCE(AVG(CAST(total_amount AS REAL)), 0) as avg_amount,
                        COALESCE(MAX(CAST(total_amount AS REAL)), 0) as max_amount,
                        COALESCE(MIN(CAST(total_amount AS REAL)), 0) as min_amount
                    FROM invoices
                    WHERE total_amount != ''
                ''')
            
            stats = dict(cursor.fetchone())
            
            # 按类型分组统计
            cursor.execute('''
                SELECT type, COUNT(*) as count, 
                       COALESCE(SUM(CAST(total_amount AS REAL)), 0) as amount
                FROM invoices
                GROUP BY type
            ''')
            stats['by_type'] = [dict(row) for row in cursor.fetchall()]
            
            # 按月份统计（最近12个月）
            cursor.execute('''
                SELECT 
                    SUBSTR(invoice_date, 1, 6) as month,
                    COUNT(*) as count,
                    COALESCE(SUM(CAST(total_amount AS REAL)), 0) as amount
                FROM invoices
                WHERE invoice_date != '' AND LENGTH(invoice_date) >= 6
                GROUP BY SUBSTR(invoice_date, 1, 6)
                ORDER BY month DESC
                LIMIT 12
            ''')
            stats['by_month'] = [dict(row) for row in cursor.fetchall()]
            
            # 回收站统计
            cursor.execute('SELECT COUNT(*) as count FROM recycle_bin')
            stats['recycle_bin_count'] = cursor.fetchone()['count']
        
        return jsonify({'success': True, 'data': stats})
    
    except Exception as e:
        logger.error(f'统计失败: {str(e)}')
        return jsonify({'error': f'统计失败: {str(e)}'}), 500

@app.route('/api/batch-update', methods=['POST'])
def batch_update_invoices():
    """批量更新发票"""
    try:
        data = request.json
        invoice_ids = data.get('ids', [])
        update_data = data.get('data', {})
        
        if not invoice_ids:
            return jsonify({'error': '没有选择要更新的发票'}), 400
        
        # 允许更新的字段白名单
        allowed_fields = {'buyer_name', 'type'}
        update_data = {k: v for k, v in update_data.items() if k in allowed_fields}
        
        if not update_data:
            return jsonify({'error': '没有有效的更新字段'}), 400
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            update_data['updated_at'] = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
            
            set_clause = ', '.join([f'{k} = ?' for k in update_data.keys()])
            
            for invoice_id in invoice_ids:
                values = list(update_data.values()) + [invoice_id]
                cursor.execute(f'UPDATE invoices SET {set_clause} WHERE id = ?', values)
            
            conn.commit()
            logger.info(f'批量更新了 {len(invoice_ids)} 张发票')
        
        return jsonify({'success': True, 'message': f'成功更新 {len(invoice_ids)} 张发票'})
    
    except Exception as e:
        logger.error(f'批量更新失败: {str(e)}')
        return jsonify({'error': f'批量更新失败: {str(e)}'}), 500

@app.route('/api/download/<filename>', methods=['GET'])
def download_pdf(filename):
    """下载 PDF 文件"""
    try:
        # 安全性检查：防止路径遍历攻击
        if '..' in filename or filename.startswith('/'):
            return jsonify({'error': '非法文件名'}), 400
        
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)
    except FileNotFoundError:
        return jsonify({'error': '文件不存在'}), 404
    except Exception as e:
        logger.error(f'下载失败: {str(e)}')
        return jsonify({'error': f'下载失败: {str(e)}'}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM invoices')
            count = cursor.fetchone()[0]
        
        return jsonify({
            'status': 'healthy',
            'database': 'connected',
            'invoice_count': count,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500

# 错误处理器
@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'error': '文件太大，最大允许 16MB'}), 413

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': '接口不存在'}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f'服务器错误: {str(error)}')
    return jsonify({'error': '服务器内部错误'}), 500

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    
    logger.info(f'启动服务器: http://{host}:{port}')
    logger.info(f'调试模式: {debug_mode}')
    logger.info(f'OCR 预加载: {app.config["PRELOAD_OCR"]}')
    
    app.run(debug=debug_mode, host=host, port=port)
