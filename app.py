import os
import re
import io
import json
import uuid
import base64
import shutil
import zipfile
from pathlib import Path
from typing import Dict, Tuple, List

from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash
from werkzeug.utils import secure_filename
import fitz  # PyMuPDF
import pytesseract
from PIL import Image
from pypdf import PdfReader, PdfWriter

BASE_DIR = Path(__file__).resolve().parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production")
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024  # 80MB


# ---------- Helpers ----------
def cleanup_old_sessions(max_age_hours: int = 12) -> None:
    import time
    now = time.time()
    max_age = max_age_hours * 3600
    for item in TMP_DIR.iterdir():
        try:
            if item.is_dir() and now - item.stat().st_mtime > max_age:
                shutil.rmtree(item, ignore_errors=True)
        except Exception:
            pass


def sanitize_text(s: str) -> str:
    s = s or ""
    s = s.replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def sanitize_filename_piece(s: str) -> str:
    s = sanitize_text(s)
    s = re.sub(r'[<>:"/\\|?*]', "", s)
    s = s.strip(" .-_")
    return s[:180]


def format_cnpj(cnpj: str) -> str:
    digits = re.sub(r"\D", "", cnpj or "")
    if len(digits) != 14:
        return ""
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


def validate_cnpj(cnpj: str) -> bool:
    digits = re.sub(r"\D", "", cnpj or "")
    if len(digits) != 14 or digits == digits[0] * 14:
        return False
    def calc(base: str, factors: List[int]) -> str:
        total = sum(int(n) * f for n, f in zip(base, factors))
        mod = total % 11
        return "0" if mod < 2 else str(11 - mod)
    d1 = calc(digits[:12], [5,4,3,2,9,8,7,6,5,4,3,2])
    d2 = calc(digits[:12] + d1, [6,5,4,3,2,9,8,7,6,5,4,3,2])
    return digits[-2:] == d1 + d2


def build_filename(funcionario: str, empresa: str, cnpj: str) -> str:
    funcionario = sanitize_filename_piece(funcionario) or "SEM_NOME"
    empresa = sanitize_filename_piece(empresa) or "SEM_EMPRESA"
    cnpj_fmt = format_cnpj(cnpj)
    if cnpj_fmt:
        return f"{funcionario} - {empresa} - {cnpj_fmt}.pdf"
    return f"{funcionario} - {empresa}.pdf"


def image_to_base64_png(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def page_to_image(page: fitz.Page, zoom: float = 1.25) -> Image.Image:
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def extract_text(page: fitz.Page) -> str:
    text = sanitize_text(page.get_text("text"))
    if len(text) >= 40:
        return text
    # fallback OCR
    try:
        img = page_to_image(page, zoom=2.0)
        ocr = pytesseract.image_to_string(img, lang="por")
        return sanitize_text(ocr)
    except Exception:
        return text


def clean_empresa(s: str) -> str:
    s = sanitize_text(s)
    s = re.sub(r"^(DADOS DA EMPRESA\s*/\s*COLABORADOR)\b", "", s, flags=re.I)
    s = re.sub(r"\s*/\s*COLABORADOR\b", "", s, flags=re.I)
    s = re.sub(r"\bCod:\s*\d+\b", "", s, flags=re.I)
    s = re.sub(r"\bCNPJ\b.*$", "", s, flags=re.I)
    s = sanitize_text(s)
    return s


def clean_funcionario(s: str) -> str:
    s = sanitize_text(s)
    s = re.sub(r"\bCod:\s*\d+\b", "", s, flags=re.I)
    s = sanitize_text(s)
    return s


def extract_suggestion(text: str) -> Dict[str, str]:
    normalized = text.replace("ﬁ", "fi").replace("ﬂ", "fl")
    txt = normalized

    empresa = ""
    funcionario = ""
    cnpj = ""

    # CNPJ first
    m_cnpj = re.search(r"(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})", txt)
    if m_cnpj:
        cnpj = re.sub(r"\D", "", m_cnpj.group(1))

    # ASO normal / audiometria
    m_empresa = re.search(r"\bEmpresa\s*:\s*(.+?)(?:\bCNPJ\b|\bFuncion[aá]rio\b|$)", txt, re.I)
    if m_empresa:
        empresa = clean_empresa(m_empresa.group(1))

    m_func = re.search(r"\bFuncion[aá]rio\s*:\s*(.+?)(?:\bCod\b|$)", txt, re.I)
    if m_func:
        funcionario = clean_funcionario(m_func.group(1))

    # Espirometria / Acuidade / other models
    if not funcionario:
        m_nome = re.search(r"\bNome\s*:\s*(.+?)(?:\bIdade\b|\bData\b|\bSexo\b|$)", txt, re.I)
        if m_nome:
            funcionario = clean_funcionario(m_nome.group(1))

    if not empresa:
        m_conv = re.search(r"\bConv[eê]nio\s*:\s*(.+?)(?:\bCNPJ\b|\bCPF\b|\bData\b|$)", txt, re.I)
        if m_conv:
            empresa = clean_empresa(m_conv.group(1))

    # fallback line-based heuristics
    lines = [sanitize_text(x) for x in re.split(r"[\r\n]+", normalized) if sanitize_text(x)]
    if not empresa:
        for i, line in enumerate(lines):
            if re.search(r"\bEmpresa\s*:", line, re.I):
                empresa = clean_empresa(re.sub(r"^.*?\bEmpresa\s*:\s*", "", line, flags=re.I))
                break
    if not funcionario:
        for i, line in enumerate(lines):
            if re.search(r"\bFuncion[aá]rio\s*:", line, re.I):
                funcionario = clean_funcionario(re.sub(r"^.*?\bFuncion[aá]rio\s*:\s*", "", line, flags=re.I))
                break

    empresa = sanitize_text(empresa)
    funcionario = sanitize_text(funcionario)
    cnpj_fmt = format_cnpj(cnpj) if validate_cnpj(cnpj) else (format_cnpj(cnpj) if re.sub(r"\D", "", cnpj) else "")

    return {
        "funcionario": funcionario,
        "empresa": empresa,
        "cnpj": cnpj_fmt,
        "filename": build_filename(funcionario, empresa, cnpj_fmt),
        "cnpj_valid": validate_cnpj(cnpj_fmt) if cnpj_fmt else False,
    }


def get_session_dir() -> Path:
    sid = session.get("sid")
    if not sid:
        sid = str(uuid.uuid4())
        session["sid"] = sid
    path = TMP_DIR / sid
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifest_path() -> Path:
    return get_session_dir() / "manifest.json"


def save_manifest(data: Dict) -> None:
    manifest_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_manifest() -> Dict:
    path = manifest_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ---------- Routes ----------
@app.before_request
def _housekeeping():
    cleanup_old_sessions()


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("pdfs")
    files = [f for f in files if f and f.filename]
    if not files:
        flash("Envie pelo menos um PDF.", "danger")
        return redirect(url_for("index"))

    session_dir = get_session_dir()
    # reset prior session files
    for item in session_dir.iterdir():
        if item.is_file():
            item.unlink(missing_ok=True)
        elif item.is_dir():
            shutil.rmtree(item, ignore_errors=True)

    pages = []
    pdf_dir = session_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    for file_idx, file in enumerate(files):
        filename = secure_filename(file.filename) or f"arquivo_{file_idx+1}.pdf"
        pdf_path = pdf_dir / filename
        file.save(pdf_path)

        doc = fitz.open(pdf_path)
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            text = extract_text(page)
            suggestion = extract_suggestion(text)
            img = page_to_image(page, zoom=0.7)
            thumb_b64 = image_to_base64_png(img)

            pages.append({
                "id": f"{file_idx}_{page_index}",
                "pdf_name": filename,
                "pdf_path": str(pdf_path),
                "page_index": page_index,
                "page_label": page_index + 1,
                "thumb_b64": thumb_b64,
                "ocr_text": text[:6000],
                **suggestion
            })
        doc.close()

    manifest = {"pages": pages}
    save_manifest(manifest)
    return render_template("review.html", pages=pages, total=len(pages))


@app.route("/generate", methods=["POST"])
def generate():
    manifest = load_manifest()
    known = {p["id"]: p for p in manifest.get("pages", [])}
    selected_ids = request.form.getlist("selected")
    if not selected_ids:
        flash("Selecione pelo menos uma página.", "danger")
        return redirect(url_for("index"))

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        used_names = {}

        for page_id in selected_ids:
            row = known.get(page_id)
            if not row:
                continue

            funcionario = request.form.get(f"funcionario__{page_id}", row.get("funcionario", ""))
            empresa = request.form.get(f"empresa__{page_id}", row.get("empresa", ""))
            cnpj = request.form.get(f"cnpj__{page_id}", row.get("cnpj", ""))

            cnpj_digits = re.sub(r"\D", "", cnpj or "")
            cnpj_fmt = format_cnpj(cnpj_digits) if len(cnpj_digits) == 14 else ""

            filename = build_filename(funcionario, empresa, cnpj_fmt)
            stem, ext = os.path.splitext(filename)
            count = used_names.get(filename, 0)
            if count:
                filename = f"{stem} ({count+1}){ext}"
            used_names[build_filename(funcionario, empresa, cnpj_fmt)] = count + 1

            src_path = Path(row["pdf_path"])
            reader = PdfReader(str(src_path))
            writer = PdfWriter()
            writer.add_page(reader.pages[int(row["page_index"])])

            pdf_bytes = io.BytesIO()
            writer.write(pdf_bytes)
            zf.writestr(filename, pdf_bytes.getvalue())

    zip_buffer.seek(0)
    return send_file(zip_buffer, as_attachment=True, download_name="ASOS_SEPARADOS.zip", mimetype="application/zip")


@app.route("/healthz", methods=["GET"])
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
