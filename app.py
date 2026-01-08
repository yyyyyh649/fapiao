import os
import json
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, send_from_directory
from werkzeug.utils import secure_filename
from paddleocr import PaddleOCR
from pdf2image import convert_from_path
import re
from PIL import Image

app = Flask(__name__, static_folder='static', static_url_path='')
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Initialize PaddleOCR
ocr = PaddleOCR(use_angle_cls=True, lang='ch', use_gpu=False)

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Database initialization
def init_db():
    conn = sqlite3.connect('invoices.db')
    cursor = conn.cursor()
    
    # Create invoices table
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create recycle_bin table
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
            deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

def extract_invoice_info(image_path):
    """Extract invoice information using OCR"""
    result = ocr.ocr(image_path, cls=True)
    
    # Extract all text
    all_text = []
    for line in result:
        for word_info in line:
            all_text.append(word_info[1][0])
    
    full_text = ' '.join(all_text)
    
    # Initialize extracted data
    invoice_data = {
        'invoice_number': '',
        'invoice_date': '',
        'total_amount': '',
        'invoice_content': '',
        'seller_name': '',
        'bank_name': '',
        'bank_account': ''
    }
    
    # Extract invoice number (typically 8 digits)
    invoice_num_pattern = r'发票号码.*?(\d{8})'
    invoice_num_match = re.search(invoice_num_pattern, full_text)
    if invoice_num_match:
        invoice_data['invoice_number'] = invoice_num_match.group(1)
    
    # Extract date (format: YYYYMMDD or YYYY年MM月DD日)
    date_patterns = [
        r'(\d{4}年\d{1,2}月\d{1,2}日)',
        r'(\d{8})',
        r'开票日期.*?(\d{4}年\d{1,2}月\d{1,2}日)',
        r'开票日期.*?(\d{8})'
    ]
    for pattern in date_patterns:
        date_match = re.search(pattern, full_text)
        if date_match:
            date_str = date_match.group(1)
            # Convert to YYYYMMDD format
            if '年' in date_str:
                date_str = re.sub(r'年|月', '', date_str).replace('日', '')
                if len(date_str) == 7:  # Single digit month or day
                    parts = re.findall(r'\d+', date_match.group(1))
                    if len(parts) == 3:
                        date_str = f"{parts[0]}{parts[1].zfill(2)}{parts[2].zfill(2)}"
            invoice_data['invoice_date'] = date_str
            break
    
    # Extract total amount
    amount_patterns = [
        r'价税合计.*?¥\s*([\d,]+\.?\d*)',
        r'价税合计.*?([\d,]+\.?\d*)',
        r'合计.*?¥\s*([\d,]+\.?\d*)',
        r'总金额.*?¥\s*([\d,]+\.?\d*)'
    ]
    for pattern in amount_patterns:
        amount_match = re.search(pattern, full_text)
        if amount_match:
            invoice_data['total_amount'] = amount_match.group(1).replace(',', '')
            break
    
    # Extract invoice content (items purchased)
    content_patterns = [
        r'货物或应税劳务名称.*?([^\d¥]{2,50})',
        r'项目名称.*?([^\d¥]{2,50})',
    ]
    for pattern in content_patterns:
        content_match = re.search(pattern, full_text)
        if content_match:
            invoice_data['invoice_content'] = content_match.group(1).strip()
            break
    
    # Extract seller name
    seller_patterns = [
        r'销售方名称.*?[:：]?\s*([^\n]{4,50})',
        r'销售方.*?[:：]?\s*([^\n]{4,50})',
    ]
    for pattern in seller_patterns:
        seller_match = re.search(pattern, full_text)
        if seller_match:
            seller_name = seller_match.group(1).strip()
            # Clean up the seller name
            seller_name = re.sub(r'纳税人识别号.*', '', seller_name).strip()
            invoice_data['seller_name'] = seller_name
            break
    
    # Extract bank name and account
    bank_patterns = [
        r'开户行.*?[:：]?\s*([^\n]{4,50})',
        r'开户银行.*?[:：]?\s*([^\n]{4,50})',
    ]
    for pattern in bank_patterns:
        bank_match = re.search(pattern, full_text)
        if bank_match:
            invoice_data['bank_name'] = bank_match.group(1).strip()
            break
    
    account_patterns = [
        r'账号.*?[:：]?\s*(\d{10,30})',
        r'银行账号.*?[:：]?\s*(\d{10,30})',
    ]
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
    """Upload and process invoice PDF"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': '没有上传文件'}), 400
        
        file = request.files['file']
        invoice_type = request.form.get('type')
        buyer_name = request.form.get('buyer_name')
        
        if file.filename == '':
            return jsonify({'error': '没有选择文件'}), 400
        
        if not invoice_type or not buyer_name:
            return jsonify({'error': '请填写完整信息'}), 400
        
        if file and file.filename.lower().endswith('.pdf'):
            filename = secure_filename(file.filename)
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            filename = f"{timestamp}_{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            # Convert PDF to image for OCR
            try:
                images = convert_from_path(filepath, first_page=1, last_page=1)
                if images:
                    # Save first page as image
                    img_path = filepath.replace('.pdf', '.jpg')
                    images[0].save(img_path, 'JPEG')
                    
                    # Extract invoice information
                    invoice_data = extract_invoice_info(img_path)
                    
                    # Remove temporary image
                    os.remove(img_path)
                    
                    # Save to database
                    conn = sqlite3.connect('invoices.db')
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT INTO invoices (type, buyer_name, invoice_number, invoice_date, 
                                            total_amount, invoice_content, seller_name, 
                                            bank_name, bank_account, pdf_path)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (invoice_type, buyer_name, invoice_data['invoice_number'],
                          invoice_data['invoice_date'], invoice_data['total_amount'],
                          invoice_data['invoice_content'], invoice_data['seller_name'],
                          invoice_data['bank_name'], invoice_data['bank_account'], filename))
                    conn.commit()
                    conn.close()
                    
                    return jsonify({
                        'success': True,
                        'message': '发票上传成功',
                        'data': invoice_data
                    })
            except Exception as e:
                return jsonify({'error': f'PDF处理失败: {str(e)}'}), 500
        
        return jsonify({'error': '请上传PDF文件'}), 400
    
    except Exception as e:
        return jsonify({'error': f'上传失败: {str(e)}'}), 500

@app.route('/api/invoices/<invoice_type>', methods=['GET'])
def get_invoices(invoice_type):
    """Get invoices by type"""
    try:
        conn = sqlite3.connect('invoices.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount,
                   invoice_content, seller_name, bank_name, bank_account, pdf_path
            FROM invoices
            WHERE type = ?
            ORDER BY created_at DESC
        ''', (invoice_type,))
        
        rows = cursor.fetchall()
        invoices = [dict(row) for row in rows]
        conn.close()
        
        return jsonify({'success': True, 'data': invoices})
    
    except Exception as e:
        return jsonify({'error': f'查询失败: {str(e)}'}), 500

@app.route('/api/invoices/delete', methods=['POST'])
def delete_invoices():
    """Move invoices to recycle bin"""
    try:
        data = request.json
        invoice_ids = data.get('ids', [])
        
        if not invoice_ids:
            return jsonify({'error': '没有选择要删除的发票'}), 400
        
        conn = sqlite3.connect('invoices.db')
        cursor = conn.cursor()
        
        # Move to recycle bin
        for invoice_id in invoice_ids:
            cursor.execute('''
                INSERT INTO recycle_bin (type, buyer_name, invoice_number, invoice_date,
                                        total_amount, invoice_content, seller_name,
                                        bank_name, bank_account, pdf_path)
                SELECT type, buyer_name, invoice_number, invoice_date, total_amount,
                       invoice_content, seller_name, bank_name, bank_account, pdf_path
                FROM invoices
                WHERE id = ?
            ''', (invoice_id,))
            
            cursor.execute('DELETE FROM invoices WHERE id = ?', (invoice_id,))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': '删除成功'})
    
    except Exception as e:
        return jsonify({'error': f'删除失败: {str(e)}'}), 500

@app.route('/api/recycle-bin/<invoice_type>', methods=['GET'])
def get_recycle_bin(invoice_type):
    """Get deleted invoices from recycle bin"""
    try:
        conn = sqlite3.connect('invoices.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Clean up old records (>30 days)
        thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('DELETE FROM recycle_bin WHERE deleted_at < ?', (thirty_days_ago,))
        
        cursor.execute('''
            SELECT id, type, buyer_name, invoice_number, invoice_date, total_amount,
                   invoice_content, seller_name, bank_name, bank_account, pdf_path, deleted_at
            FROM recycle_bin
            WHERE type = ?
            ORDER BY deleted_at DESC
        ''', (invoice_type,))
        
        rows = cursor.fetchall()
        invoices = [dict(row) for row in rows]
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'data': invoices})
    
    except Exception as e:
        return jsonify({'error': f'查询失败: {str(e)}'}), 500

@app.route('/api/download/<filename>', methods=['GET'])
def download_pdf(filename):
    """Download PDF file"""
    try:
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)
    except Exception as e:
        return jsonify({'error': f'下载失败: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
