# 发票管理系统 (Invoice Management System)

可以识别发票，自动输出内容为表格

## 功能特性

- **上传发票信息**：支持自费和对公两种类型，上传PDF格式的发票
- **自动OCR识别**：自动识别发票号码、开票日期、价税总金额、发票内容、销售方户名、开户银行名和银行账号
- **查看发票信息**：分别查看自费和汇款类型的发票，支持复选框选择
- **批量操作**：支持全选、删除选中内容和下载选中PDF
- **回收站**：删除的发票进入回收站，超过30天自动清除

## 安装依赖

### 系统依赖

Ubuntu/Debian:
```bash
sudo apt-get update
sudo apt-get install -y poppler-utils
```

macOS:
```bash
brew install poppler
```

### Python依赖

```bash
pip install -r requirements.txt
```

## 运行应用

```bash
python app.py
```

应用将在 http://localhost:5000 启动

开发模式（可选，仅用于开发调试）：
```bash
export FLASK_DEBUG=true
python app.py
```

## 使用说明

1. **上传发票**
   - 选择"上传发票信息"
   - 选择发票类型（自费或对公）
   - 上传PDF格式的发票
   - 填写购买人姓名（自费请填写自己的名字）
   - 系统将自动识别发票信息

2. **查看发票**
   - 选择"查看发票信息"
   - 选择类型（自费或汇款）
   - 查看发票列表
   - 可以全选、删除或下载PDF

3. **回收站**
   - 选择"回收站"
   - 选择类型（自费或汇款）
   - 查看已删除的发票
   - 超过30天的记录会自动清除

## 技术栈

- **后端**：Flask (Python)
- **前端**：HTML, CSS, JavaScript
- **OCR引擎**：PaddleOCR
- **数据库**：SQLite
- **PDF处理**：pdf2image
