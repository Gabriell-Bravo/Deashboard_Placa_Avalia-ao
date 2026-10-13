import os
import re
import secrets
from datetime import datetime
from functools import wraps
from io import BytesIO
from urllib.parse import urlparse

import qrcode
from flask import (
    Flask,
    Response,
    flash,
    g,
    has_request_context,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from db import Database, init_schema, integrity_error_types, persistent_data, using_postgres
from placa import SIDE_PX, placa_pdf_bytes, placa_png_bytes
from qrcode.constants import ERROR_CORRECT_M

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SECRET_KEY = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or ""
).rstrip("/")
PERSISTENT_DATA = persistent_data()
IntegrityError = integrity_error_types()

app = Flask(__name__)
app.secret_key = SECRET_KEY


def get_db() -> Database:
    if "db" not in g:
        g.db = Database()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    init_schema(get_db())

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def generate_code() -> str:
    db = get_db()
    for _ in range(80):
        code = f"PLACA-{secrets.randbelow(9000) + 1000}"
        exists = db.execute("SELECT 1 FROM qrcodes WHERE code = ?", (code,)).fetchone()
        if not exists:
            return code
    raise RuntimeError("Não foi possível gerar um código único.")


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("Informe o link de destino.")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("Link inválido.")
    return url


def parse_money(value: str) -> float:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Informe o valor da venda.")
    raw = raw.replace("R$", "").replace(" ", "")
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        amount = float(raw)
    except ValueError as exc:
        raise ValueError("Valor inválido. Use o formato 150,00") from exc
    if amount < 0:
        raise ValueError("O valor não pode ser negativo.")
    return round(amount, 2)


def format_money(value) -> str:
    if value is None:
        return "R$ 0,00"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "R$ 0,00"
    formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def destination_label(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    path = (urlparse(url).path or "").lower()
    full = f"{host}{path}"
    if any(x in full for x in ("google.", "g.page", "goo.gl", "maps.app")):
        return "Google Avaliações"
    if "instagram.com" in host:
        return "Instagram"
    if "wa.me" in host or "whatsapp" in host:
        return "WhatsApp"
    if "facebook.com" in host or "fb.com" in host:
        return "Facebook"
    return "Link personalizado"


def format_last_scan(value: str | None) -> str:
    if not value:
        return "Nenhuma leitura"
    try:
        dt = datetime.fromisoformat(value.replace("Z", ""))
        return dt.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return value


def public_base_url() -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    return request.url_root.rstrip("/")


def public_qr_url(code: str) -> str:
    return f"{public_base_url()}/r/{code}"


def is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("192.168.")


def enrich_item(row) -> dict:
    item = dict(row)
    item["destination_label"] = destination_label(item["target_url"])
    item["last_scan_label"] = format_last_scan(item.get("last_scan"))
    if item.get("scans") and not item.get("last_scan"):
        item["last_scan_label"] = "Antes do registro de data"
    item["sale_price_label"] = format_money(item.get("sale_price"))
    item["sold_at_label"] = format_last_scan(item.get("sold_at")) if item.get("sold_at") else None
    return item


def dashboard_stats(db):
    return db.execute(
        """
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(scans), 0) AS scans,
            COALESCE(SUM(CASE WHEN sold = 1 THEN 1 ELSE 0 END), 0) AS sold,
            COALESCE(SUM(CASE WHEN sold = 0 THEN 1 ELSE 0 END), 0) AS available,
            COALESCE(SUM(CASE WHEN sold = 1 THEN sale_price ELSE 0 END), 0) AS revenue
        FROM qrcodes
        """
    ).fetchone()


@app.context_processor
def inject_helpers():
    base = public_base_url() if has_request_context() else (PUBLIC_BASE_URL or "")
    return {
        "destination_label": destination_label,
        "format_last_scan": format_last_scan,
        "format_money": format_money,
        "public_base": base,
        "is_local_host": is_local_url(base) if base else True,
        "persistent_data": PERSISTENT_DATA,
        "using_postgres": using_postgres(),
    }


def build_qr_image(payload: str, with_label: bool = True, code: str = ""):
    from PIL import Image, ImageDraw, ImageFont

    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=12,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    if not with_label:
        return img

    label = code or ""
    label_h = 56
    w, h = img.size
    canvas = Image.new("RGB", (w, h + label_h), "white")
    canvas.paste(img, (0, 0))
    draw = ImageDraw.Draw(canvas)

    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 28)
    except OSError:
        try:
            font = ImageFont.truetype("arial.ttf", 28)
        except OSError:
            font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (w - tw) // 2
    y = h + (label_h - th) // 2 - 2
    draw.text((x, y), label, fill="#111827", font=font)
    return canvas


@app.before_request
def ensure_db():
    init_db()


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = "Senha incorreta."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/backup.json")
@login_required
def export_backup():
    import json

    db = get_db()
    rows = db.execute("SELECT * FROM qrcodes ORDER BY id").fetchall()
    payload = {
        "exported_at": now_iso(),
        "items": [dict(row) for row in rows],
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return send_file(
        BytesIO(data),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"backup-placas-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json",
    )


@app.route("/backup/import", methods=["POST"])
@login_required
def import_backup():
    import json

    upload = request.files.get("backup_file")
    if not upload or not upload.filename:
        flash("Selecione um arquivo de backup JSON.", "error")
        return redirect(url_for("dashboard"))
    try:
        payload = json.load(upload.stream)
        items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise ValueError("Formato inválido.")
    except Exception:
        flash("Não foi possível ler o backup. Use o JSON exportado pelo sistema.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    imported = 0
    for raw in items:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip().upper()
        name = str(raw.get("name") or "").strip()
        target_url = str(raw.get("target_url") or "").strip()
        if not code or not name or not target_url:
            continue
        try:
            target_url = normalize_url(target_url)
        except ValueError:
            continue

        now = now_iso()
        company = (raw.get("company") or "").strip()
        notes = (raw.get("notes") or "").strip()
        scans = int(raw.get("scans") or 0)
        last_scan = raw.get("last_scan")
        active = 1 if raw.get("active", 1) else 0
        sold = 1 if raw.get("sold") else 0
        sale_price = raw.get("sale_price")
        sold_at = raw.get("sold_at")
        created_at = raw.get("created_at") or now

        exists = db.execute("SELECT id FROM qrcodes WHERE code = ?", (code,)).fetchone()
        if exists:
            db.execute(
                """
                UPDATE qrcodes
                SET name=?, company=?, target_url=?, notes=?, scans=?, last_scan=?,
                    active=?, sold=?, sale_price=?, sold_at=?, updated_at=?
                WHERE code=?
                """,
                (
                    name,
                    company,
                    target_url,
                    notes,
                    scans,
                    last_scan,
                    active,
                    sold,
                    sale_price,
                    sold_at,
                    now,
                    code,
                ),
            )
        else:
            db.execute(
                """
                INSERT INTO qrcodes (
                    code, name, company, target_url, notes, scans, last_scan,
                    active, sold, sale_price, sold_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    name,
                    company,
                    target_url,
                    notes,
                    scans,
                    last_scan,
                    active,
                    sold,
                    sale_price,
                    sold_at,
                    created_at,
                    now,
                ),
            )
        imported += 1
    db.commit()
    flash(f"Backup importado: {imported} placa(s).", "success")
    return redirect(url_for("dashboard"))


@app.route("/")
@login_required
def dashboard():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    sql = "SELECT * FROM qrcodes"
    params: list = []
    if q:
        sql += " WHERE code LIKE ? OR name LIKE ? OR company LIKE ? OR target_url LIKE ?"
        like = f"%{q}%"
        params.extend([like, like, like, like])
    sql += " ORDER BY created_at DESC"
    rows = [enrich_item(row) for row in db.execute(sql, params).fetchall()]

    stats = dashboard_stats(db)

    return render_template(
        "dashboard.html",
        items=rows,
        stats=stats,
        q=q,
        base_url=public_base_url(),
    )


@app.route("/qrcodes/new", methods=["GET", "POST"])
@login_required
def create_qr():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        company = (request.form.get("company") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        custom_code = (request.form.get("code") or "").strip().upper()
        try:
            target_url = normalize_url(request.form.get("target_url"))
            if not name:
                raise ValueError("Informe um nome para a placa.")
            if custom_code:
                if not re.fullmatch(r"PLACA-\d{4,8}", custom_code):
                    raise ValueError("Código deve estar no formato PLACA-8177.")
                code = custom_code
            else:
                code = generate_code()

            now = now_iso()
            db = get_db()
            db.execute(
                """
                INSERT INTO qrcodes (code, name, company, target_url, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (code, name, company, target_url, notes, now, now),
            )
            db.commit()
            flash(f"QR Code {code} criado com sucesso.", "success")
            return redirect(url_for("dashboard"))
        except IntegrityError:
            flash("Este código já existe. Escolha outro.", "error")
        except ValueError as exc:
            flash(str(exc), "error")

    return render_template("form.html", item=None, mode="create")


@app.route("/qrcodes/<code>", methods=["GET", "POST"])
@login_required
def qr_detail(code: str):
    db = get_db()
    item = db.execute("SELECT * FROM qrcodes WHERE code = ?", (code.upper(),)).fetchone()
    if not item:
        flash("QR Code não encontrado.", "error")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        company = (request.form.get("company") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        active = 1 if request.form.get("active") == "on" else 0
        try:
            target_url = normalize_url(request.form.get("target_url"))
            if not name:
                raise ValueError("Informe um nome para a placa.")
            now = now_iso()
            db.execute(
                """
                UPDATE qrcodes
                SET name = ?, company = ?, target_url = ?, notes = ?, active = ?, updated_at = ?
                WHERE code = ?
                """,
                (name, company, target_url, notes, active, now, item["code"]),
            )
            db.commit()
            flash("Alterações salvas. O QR impresso já aponta para o novo link.", "success")
            return redirect(url_for("qr_detail", code=item["code"]))
        except ValueError as exc:
            flash(str(exc), "error")
            item = enrich_item(item)
            item.update(
                {
                    "name": name,
                    "company": company,
                    "target_url": request.form.get("target_url"),
                    "notes": notes,
                    "active": active,
                }
            )
            return render_template(
                "detail.html",
                item=item,
                public_url=public_qr_url(item["code"]),
            )

    return render_template(
        "detail.html",
        item=enrich_item(item),
        public_url=public_qr_url(item["code"]),
    )


@app.route("/qrcodes/<code>/toggle", methods=["POST"])
@login_required
def toggle_qr(code: str):
    db = get_db()
    item = db.execute("SELECT * FROM qrcodes WHERE code = ?", (code.upper(),)).fetchone()
    if not item:
        flash("QR Code não encontrado.", "error")
        return redirect(url_for("dashboard"))
    new_active = 0 if item["active"] else 1
    db.execute(
        "UPDATE qrcodes SET active = ?, updated_at = ? WHERE code = ?",
        (new_active, now_iso(), item["code"]),
    )
    db.commit()
    flash(
        f"{item['code']} {'ligada' if new_active else 'desligada'}.",
        "success",
    )
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/qrcodes/<code>/sell", methods=["POST"])
@login_required
def sell_qr(code: str):
    db = get_db()
    item = db.execute("SELECT * FROM qrcodes WHERE code = ?", (code.upper(),)).fetchone()
    if not item:
        flash("QR Code não encontrado.", "error")
        return redirect(url_for("dashboard"))

    try:
        price = parse_money(request.form.get("sale_price"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(request.referrer or url_for("dashboard"))

    now = now_iso()
    db.execute(
        """
        UPDATE qrcodes
        SET sold = 1, sale_price = ?, sold_at = ?, updated_at = ?
        WHERE code = ?
        """,
        (price, now, now, item["code"]),
    )
    db.commit()
    flash(f"{item['code']} marcada como vendida por {format_money(price)}.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/qrcodes/<code>/unsell", methods=["POST"])
@login_required
def unsell_qr(code: str):
    db = get_db()
    item = db.execute("SELECT * FROM qrcodes WHERE code = ?", (code.upper(),)).fetchone()
    if not item:
        flash("QR Code não encontrado.", "error")
        return redirect(url_for("dashboard"))

    db.execute(
        """
        UPDATE qrcodes
        SET sold = 0, sale_price = NULL, sold_at = NULL, updated_at = ?
        WHERE code = ?
        """,
        (now_iso(), item["code"]),
    )
    db.commit()
    flash(f"{item['code']} voltou para disponível.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/qrcodes/<code>/delete", methods=["POST"])
@login_required
def delete_qr(code: str):
    db = get_db()
    db.execute("DELETE FROM qrcodes WHERE code = ?", (code.upper(),))
    db.commit()
    flash("QR Code removido.", "success")
    return redirect(url_for("dashboard"))


@app.route("/qrcodes/<code>/image.png")
@login_required
def qr_image(code: str):
    db = get_db()
    item = db.execute("SELECT * FROM qrcodes WHERE code = ?", (code.upper(),)).fetchone()
    if not item:
        return Response("Não encontrado", status=404)

    label = request.args.get("label", "1") != "0"
    img = build_qr_image(public_qr_url(item["code"]), with_label=label, code=item["code"])
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(
        buf,
        mimetype="image/png",
        as_attachment=request.args.get("download") == "1",
        download_name=f"{item['code']}.png",
    )


@app.route("/qrcodes/<code>/placa.png")
@login_required
def placa_png(code: str):
    db = get_db()
    item = db.execute("SELECT * FROM qrcodes WHERE code = ?", (code.upper(),)).fetchone()
    if not item:
        return Response("Não encontrado", status=404)
    try:
        preview = request.args.get("preview") == "1"
        width = 480 if preview else None
        if request.args.get("w"):
            try:
                width = max(120, min(SIDE_PX, int(request.args.get("w"))))
            except ValueError:
                width = 480 if preview else None
        fmt = "JPEG" if preview else "PNG"
        data = placa_png_bytes(
            public_qr_url(item["code"]),
            code=item["code"],
            max_width=width,
            fmt=fmt,
        )
    except FileNotFoundError as exc:
        return Response(str(exc), status=500)

    mime = "image/jpeg" if fmt == "JPEG" else "image/png"
    ext = "jpg" if fmt == "JPEG" else "png"
    resp = send_file(
        BytesIO(data),
        mimetype=mime,
        as_attachment=request.args.get("download") == "1",
        download_name=f"{item['code']}-plaquinha.{ext}",
    )
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@app.route("/qrcodes/<code>/placa.pdf")
@login_required
def placa_pdf(code: str):
    db = get_db()
    item = db.execute("SELECT * FROM qrcodes WHERE code = ?", (code.upper(),)).fetchone()
    if not item:
        return Response("Não encontrado", status=404)
    try:
        data = placa_pdf_bytes(public_qr_url(item["code"]), code=item["code"])
    except FileNotFoundError as exc:
        return Response(str(exc), status=500)
    return send_file(
        BytesIO(data),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{item['code']}-plaquinha.pdf",
    )


@app.route("/r/<code>")
def redirect_qr(code: str):
    db = get_db()
    item = db.execute("SELECT * FROM qrcodes WHERE code = ?", (code.upper(),)).fetchone()
    if not item:
        return render_template("public_error.html", message="QR Code inválido ou inexistente."), 404
    if not item["active"]:
        return render_template("public_error.html", message="Este QR Code está desativado."), 410

    db.execute(
        "UPDATE qrcodes SET scans = scans + 1, last_scan = ? WHERE id = ?",
        (now_iso(), item["id"]),
    )
    db.commit()
    return redirect(item["target_url"], code=302)


if __name__ == "__main__":
    with app.app_context():
        init_db()
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
