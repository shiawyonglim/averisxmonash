import os

def parse_txt(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()

def parse_pdf(path):
    try:
        import pdfplumber
        text_content = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text(layout=True)
                if text:
                    text_content.append(text)
        return "\n".join(text_content)
    except Exception as e:
        print(f"Error parsing PDF {path}: {e}")
        return ""

def parse_docx(path):
    try:
        import docx
        doc = docx.Document(path)
        content = []
        for p in doc.paragraphs:
            if p.text.strip():
                content.append(p.text.strip())
        
        for table in doc.tables:
            for row in table.rows:
                row_data = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                content.append(" | ".join(row_data))
        return "\n".join(content)
    except Exception as e:
        print(f"Error parsing DOCX {path}: {e}")
        return ""

def parse_xlsx(path):
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
        content = []
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            for row in ws.iter_rows(values_only=True):
                if any(c is not None for c in row):
                    row_data = [str(c).strip().replace("\n", " ") if c is not None else "" for c in row]
                    content.append(" | ".join(row_data))
        return "\n".join(content)
    except Exception as e:
        print(f"Error parsing XLSX {path}: {e}")
        return ""

def extract_text(path):
    if not os.path.exists(path):
        return ""
    
    if os.path.getsize(path) == 0:
        return ""
        
    ext = os.path.splitext(path)[1].lower()
    
    if ext == '.txt':
        return parse_txt(path)
    elif ext == '.pdf':
        return parse_pdf(path)
    elif ext == '.docx':
        return parse_docx(path)
    elif ext == '.xlsx':
        return parse_xlsx(path)
    else:
        return parse_txt(path)
