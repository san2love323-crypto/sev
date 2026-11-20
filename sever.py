"""독립 쇼핑몰 서버 — 화면은 index.html 한 파일, 이 파일은 그 뒤의 API.

실행:  python server.py            (서버 시작)
       python server.py set-admin  (관리자 비밀번호 설정/변경)
"""
import getpass
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from functools import wraps

from flask import Flask, g, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "shop.db")
UPLOAD_DIR = os.path.join(BASE, "uploads")
ALLOWED_IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


# ───────────────────────── 설정 (.env) ─────────────────────────
def load_env():
    path = os.path.join(BASE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def env(key, default=""):
    return os.environ.get(key, default).strip()


def env_int(key, default):
    try:
        return int(env(key, str(default)))
    except ValueError:
        return default


SHOP_NAME = env("SHOP_NAME", "아틀리에")
SHOP_TAGLINE = env("SHOP_TAGLINE", "오래 곁에 둘 물건만 고릅니다")
SHOP_INFO = env("SHOP_INFO", "")
BASE_URL = env("BASE_URL").rstrip("/")
SHIPPING_FEE = env_int("SHIPPING_FEE", 3000)
FREE_SHIPPING_OVER = env_int("FREE_SHIPPING_OVER", 50000)
DISCORD_WEBHOOK_URL = env("DISCORD_WEBHOOK_URL")

NP_MODE = env("NAVERPAY_MODE", "development")  # development | production
NP_PARTNER = env("NAVERPAY_PARTNER_ID")
NP_CLIENT_ID = env("NAVERPAY_CLIENT_ID")
NP_SECRET = env("NAVERPAY_CLIENT_SECRET")
NP_CHAIN = env("NAVERPAY_CHAIN_ID")
NP_API_DOMAIN = env("NAVERPAY_API_DOMAIN") or (
    "apis.naver.com" if NP_MODE == "production" else "dev-pub.apis.naver.com")
NAVERPAY_ON = bool(NP_PARTNER and NP_CLIENT_ID and NP_SECRET)

# ── 토스페이먼츠 자동충전 (빌링) ──
TOSS_CLIENT_KEY = env("TOSS_CLIENT_KEY")
TOSS_SECRET_KEY = env("TOSS_SECRET_KEY")
CREDIT_OPTIONS = [x for x in (env("CREDIT_OPTIONS", "10000,30000,50000").split(",")) if x.strip().isdigit()]
CREDIT_ON = bool(TOSS_CLIENT_KEY and TOSS_SECRET_KEY)

STATUS = {
    "pending": "결제 대기",
    "paid": "결제 완료 · 구매 필요",
    "purchased": "구매처 주문 완료",
    "shipped": "배송 중",
    "done": "배송 완료",
    "canceled": "취소",
    "review": "확인 필요",
}
CUSTOMER_STATUS = {
    "pending": "결제를 기다리고 있어요",
    "paid": "결제 완료 · 상품을 준비하고 있어요",
    "purchased": "상품을 준비하고 있어요",
    "shipped": "배송 중이에요",
    "done": "배송이 끝났어요",
    "canceled": "취소된 주문이에요",
    "review": "결제를 확인하고 있어요",
}


def secret_key():
    if env("SECRET_KEY"):
        return env("SECRET_KEY")
    path = os.path.join(BASE, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=secret_key(),
    MAX_CONTENT_LENGTH=40 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=BASE_URL.startswith("https://"),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 7,
)


# ───────────────────────── DB ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, price INTEGER NOT NULL, description TEXT DEFAULT '',
  image TEXT DEFAULT '', stock INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
  source_url TEXT DEFAULT '', source_note TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no TEXT UNIQUE, token TEXT, status TEXT DEFAULT 'pending',
  name TEXT, phone TEXT, postcode TEXT, addr1 TEXT, addr2 TEXT, memo TEXT,
  subtotal INTEGER, shipping INTEGER, total INTEGER,
  payment_id TEXT DEFAULT '', pay_method TEXT DEFAULT '',
  courier TEXT DEFAULT '', tracking TEXT DEFAULT '', admin_note TEXT DEFAULT '',
  created_at TEXT, paid_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER, product_id INTEGER, name TEXT, price INTEGER, qty INTEGER,
  source_url TEXT DEFAULT '', source_note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS product_images (
  id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, file TEXT, pos INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS credit_accounts (
  phone TEXT PRIMARY KEY, token TEXT, customer_key TEXT, balance INTEGER DEFAULT 0,
  auto_threshold INTEGER DEFAULT 0, auto_amount INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS credit_charges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT, order_id TEXT UNIQUE, payment_key TEXT DEFAULT '', amount INTEGER,
  status TEXT, created_at TEXT);
"""
MAX_IMAGES = 8


def connect():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_):
    con = g.pop("db", None)
    if con:
        con.close()


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    con = connect()
    con.executescript(SCHEMA)
    # 예전 버전(사진 1장)으로 만든 상품을 여러 장 구조로 옮기기
    for r in con.execute("SELECT id, image FROM products WHERE image<>'' AND id NOT IN"
                         " (SELECT product_id FROM product_images)").fetchall():
        con.execute("INSERT INTO product_images (product_id, file, pos) VALUES (?,?,0)",
                    (r["id"], r["image"]))
    con.commit()
    con.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def get_setting(key):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


# ───────────────────────── 공통 ─────────────────────────
class ApiError(Exception):
    def __init__(self, message, code=400):
        self.message, self.code = message, code


@app.errorhandler(ApiError)
def on_api_error(e):
    return jsonify(error=e.message), e.code


@app.errorhandler(404)
def on_404(_):
    return jsonify(error="찾는 것이 없어요."), 404


@app.errorhandler(413)
def on_413(_):
    return jsonify(error="사진은 한 번에 합쳐서 40MB 이하로 올려 주세요."), 413


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.headers.get("X-CSRF", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            raise ApiError("페이지를 새로고침한 뒤 다시 시도해 주세요.")


def body():
    return request.get_json(silent=True) or {}


def site_url():
    return BASE_URL or request.url_root.rstrip("/")


def admin_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not session.get("admin"):
            raise ApiError("관리자 로그인이 필요해요.", 401)
        return view(*a, **kw)
    return wrapped


def shipping_for(subtotal):
    if subtotal == 0 or (FREE_SHIPPING_OVER and subtotal >= FREE_SHIPPING_OVER):
        return 0
    return SHIPPING_FEE


def images_of(pid):
    return db().execute("SELECT * FROM product_images WHERE product_id=? ORDER BY pos, id",
                        (pid,)).fetchall()


def product_json(p, admin=False):
    imgs = images_of(p["id"])
    d = {"id": p["id"], "name": p["name"], "price": p["price"], "description": p["description"],
         "stock": p["stock"], "images": ["/uploads/" + i["file"] for i in imgs]}
    if admin:
        d.update(active=bool(p["active"]), source_url=p["source_url"], source_note=p["source_note"],
                 image_rows=[{"id": i["id"], "url": "/uploads/" + i["file"]} for i in imgs])
    return d


def cart_json(message=None):
    """세션 장바구니 → 화면용 데이터. 사라졌거나 숨긴 상품은 걸러 냄."""
    cart, clean, lines, subtotal = session.get("cart", {}), {}, [], 0
    for pid, qty in cart.items():
        p = db().execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            continue
        qty = max(1, min(int(qty), 99))
        clean[str(pid)] = qty
        lines.append({"id": p["id"], "name": p["name"], "price": p["price"], "qty": qty,
                      "sum": p["price"] * qty, "stock": p["stock"]})
        subtotal += p["price"] * qty
    session["cart"] = clean
    ship = shipping_for(subtotal)
    return {"lines": lines, "subtotal": subtotal, "shipping": ship, "total": subtotal + ship,
            "free_over": FREE_SHIPPING_OVER, "count": sum(clean.values()), "message": message}


# ───────────────────────── 화면 파일 ─────────────────────────
@app.route("/")
def index():
    return send_from_directory(BASE, "index.html", max_age=0)


@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOAD_DIR, name, max_age=86400)


# ───────────────────────── 고객용 API ─────────────────────────
@app.route("/api/state")
def api_state():
    return jsonify(csrf=csrf_token(), admin=bool(session.get("admin")), naverpay=NAVERPAY_ON,
                   credit=CREDIT_ON, options=CREDIT_OPTIONS,
                   cart_count=sum(session.get("cart", {}).values()),
                   shop={"name": SHOP_NAME, "tagline": SHOP_TAGLINE, "info": SHOP_INFO})


@app.route("/api/products")
def api_products():
    rows = db().execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC").fetchall()
    return jsonify([product_json(p) for p in rows])


@app.route("/api/products/<int:pid>")
def api_product(pid):
    p = db().execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        raise ApiError("판매가 끝났거나 없는 상품이에요.", 404)
    return jsonify(product_json(p))


@app.route("/api/cart")
def api_cart():
    return jsonify(cart_json())


@app.route("/api/cart", methods=["POST"])
def api_cart_change():
    b = body()
    pid = str(b.get("pid", ""))
    try:
        qty = int(b.get("qty", 1))
    except (TypeError, ValueError):
        qty = 1
    cart, message = dict(session.get("cart", {})), None
    p = db().execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        cart.pop(pid, None)
    else:
        want = cart.get(pid, 0) + max(qty, 1) if b.get("mode") == "add" else qty
        if want > p["stock"]:
            message = f"‘{p['name']}’은(는) {p['stock']}개까지 담을 수 있어요."
            want = p["stock"]
        if want <= 0:
            cart.pop(pid, None)
        else:
            cart[pid] = min(want, 99)
    session["cart"] = cart
    return jsonify(cart_json(message))


@app.route("/api/orders", methods=["POST"])
def api_order_create():
    c = cart_json()
    if not c["lines"]:
        raise ApiError("장바구니가 비어 있어요.")
    b = body()
    f = {k: str(b.get(k, "")).strip() for k in ("name", "phone", "postcode", "addr1", "addr2", "memo")}
    errors = []
    if not f["name"]:
        errors.append("받는 분 이름을 적어 주세요.")
    if len("".join(ch for ch in f["phone"] if ch.isdigit())) < 9:
        errors.append("연락처를 정확히 적어 주세요.")
    if not f["postcode"] or not f["addr1"]:
        errors.append("주소 찾기로 주소를 넣어 주세요.")
    for l in c["lines"]:
        if l["qty"] > l["stock"]:
            errors.append(f"‘{l['name']}’ 재고가 {l['stock']}개 남았어요. 수량을 줄여 주세요.")
    if errors:
        raise ApiError(" ".join(errors))
    order_no = datetime.now().strftime("%y%m%d") + "-" + secrets.token_hex(4).upper()
    token = secrets.token_urlsafe(16)
    con = db()
    cur = con.execute(
        "INSERT INTO orders (order_no, token, status, name, phone, postcode, addr1, addr2, memo,"
        " subtotal, shipping, total, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_no, token, "pending", f["name"][:40], f["phone"][:20], f["postcode"][:10],
         f["addr1"][:200], f["addr2"][:200], f["memo"][:200], c["subtotal"], c["shipping"],
         c["total"], now()))
    for l in c["lines"]:
        p = con.execute("SELECT * FROM products WHERE id=?", (l["id"],)).fetchone()
        con.execute("INSERT INTO order_items (order_id, product_id, name, price, qty, source_url,"
                    " source_note) VALUES (?,?,?,?,?,?,?)",
                    (cur.lastrowid, p["id"], p["name"], p["price"], l["qty"], p["source_url"],
                     p["source_note"]))
    con.commit()
    return jsonify(order_no=order_no, token=token)


def find_order(order_no, token):
    o = db().execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
    if not o or not secrets.compare_digest(o["token"], token):
        raise ApiError("주문을 찾을 수 없어요.", 404)
    return o


def order_items(order_id):
    return db().execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()


def naver_config(o, items):
    title = items[0]["name"] + (f" 외 {len(items) - 1}건" if len(items) > 1 else "")
    cfg = {
        "create": {"mode": NP_MODE, "clientId": NP_CLIENT_ID, "payType": "normal", "openType": "page"},
        "open": {
            "merchantPayKey": o["order_no"], "productName": title[:128],
            "productCount": sum(i["qty"] for i in items),
            "totalPayAmount": o["total"], "taxScopeAmount": o["total"], "taxExScopeAmount": 0,
            "returnUrl": f"{site_url()}/pay/naver/return/{o['order_no']}/{o['token']}",
            "productItems": [{"categoryType": "PRODUCT", "categoryId": "GENERAL",
                              "uid": str(i["product_id"]), "name": i["name"][:128],
                              "count": i["qty"]} for i in items],
        },
    }
    if NP_CHAIN:
        cfg["create"]["chainId"] = NP_CHAIN
    return cfg


@app.route("/api/orders/<order_no>/<token>")
def api_order(order_no, token):
    o = find_order(order_no, token)
    items = order_items(o["id"])
    d = {k: o[k] for k in ("order_no", "token", "status", "name", "phone", "postcode", "addr1",
                           "addr2", "memo", "subtotal", "shipping", "total", "courier",
                           "tracking", "created_at")}
    d["status_text"] = CUSTOMER_STATUS.get(o["status"], "")
    d["items"] = [{"name": i["name"], "price": i["price"], "qty": i["qty"]} for i in items]
    if o["status"] == "pending":
        d["naver"] = naver_config(o, items) if NAVERPAY_ON else None
    return jsonify(d)


@app.route("/api/lookup", methods=["POST"])
def api_lookup():
    b = body()
    digits = "".join(ch for ch in str(b.get("phone", "")) if ch.isdigit())
    o = db().execute("SELECT * FROM orders WHERE order_no=?",
                     (str(b.get("order_no", "")).strip().upper(),)).fetchone()
    if o and digits and digits == "".join(ch for ch in o["phone"] if ch.isdigit()):
        return jsonify(order_no=o["order_no"], token=o["token"])
    raise ApiError("주문번호와 연락처가 일치하는 주문이 없어요.", 404)


# ───────────────────────── 네이버페이 ─────────────────────────
def naver_api(path, data):
    """네이버페이 결제형 API 호출. (성공여부, 응답 dict) 반환."""
    url = f"https://{NP_API_DOMAIN}/{NP_PARTNER}/naverpay/payments/{path}"
    headers = {
        "X-Naver-Client-Id": NP_CLIENT_ID,
        "X-Naver-Client-Secret": NP_SECRET,
        "X-NaverPay-Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if NP_CHAIN:
        headers["X-NaverPay-Chain-Id"] = NP_CHAIN
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read().decode())
        except Exception:
            res = {"code": "Fail", "message": f"HTTP {e.code}"}
    except Exception as e:  # 네트워크 오류, 시간 초과
        res = {"code": "Fail", "message": f"연결 실패: {e}"}
    return res.get("code") == "Success", res


def mark_paid(order, payment_id, method):
    """pending → paid 로 한 번만 바꾸고 재고 차감, 알림. 이미 처리됐으면 False."""
    con = db()
    cur = con.execute(
        "UPDATE orders SET status='paid', payment_id=?, pay_method=?, paid_at=?"
        " WHERE id=? AND status='pending'", (payment_id, method, now(), order["id"]))
    if cur.rowcount == 0:
        con.commit()
        return False
    for i in order_items(order["id"]):
        con.execute("UPDATE products SET stock=MAX(stock-?,0) WHERE id=?",
                    (i["qty"], i["product_id"]))
    con.commit()
    notify_new_order(order)
    return True


def notify_new_order(order):
    if not DISCORD_WEBHOOK_URL:
        return
    items = order_items(order["id"])
    summary = ", ".join(f"{i['name']} ×{i['qty']}" for i in items)
    link = f"{site_url()}/#/admin/order/{order['id']}"
    body = json.dumps({"content": f"🛎 새 주문 {order['order_no']}\n{summary}\n"
                                  f"결제 {order['total']:,}원\n처리하기: {link}"}).encode()

    def send():
        try:
            req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "shop-notify"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print("알림 전송 실패:", e)

    threading.Thread(target=send, daemon=True).start()


def back_to_order(order_no, token, message=""):
    q = "?m=" + urllib.parse.quote(message) if message else ""
    return redirect(f"/{q}#/order/{order_no}/{token}")


@app.route("/pay/naver/return/<order_no>/<token>")
def naver_return(order_no, token):
    o = db().execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
    if not o or not secrets.compare_digest(o["token"], token):
        return redirect("/")
    if not NAVERPAY_ON or o["status"] != "pending":
        return back_to_order(order_no, token)
    if request.args.get("resultCode") != "Success":
        return back_to_order(order_no, token, request.args.get("resultMessage")
                             or "결제가 완료되지 않았어요. 다시 시도해 주세요.")
    payment_id = request.args.get("paymentId", "")
    used = db().execute("SELECT id FROM orders WHERE payment_id=? AND id<>?",
                        (payment_id, o["id"])).fetchone()
    if not payment_id or used:
        return back_to_order(order_no, token, "결제 정보를 확인할 수 없어요.")

    ok, res = naver_api("v2.2/apply/payment", {"paymentId": payment_id})
    if not ok:
        return back_to_order(order_no, token, "결제 승인에 실패했어요: " + str(res.get("message", "")))

    detail = (res.get("body") or {}).get("detail") or {}
    paid_amount = int(detail.get("totalPayAmount") or 0)
    key = detail.get("merchantPayKey") or o["order_no"]
    if paid_amount != o["total"] or key != o["order_no"]:
        # 결제된 금액/주문번호가 주문과 다름 → 즉시 취소 시도 후 관리자 확인으로 넘김
        c_ok, c_res = naver_api("v1/cancel", {
            "paymentId": payment_id, "cancelAmount": paid_amount,
            "cancelReason": "주문 금액 불일치", "cancelRequester": "2",
            "taxScopeAmount": paid_amount, "taxExScopeAmount": 0})
        cancelled = c_ok or c_res.get("code") == "CancelNotComplete"
        note = (f"[자동] 결제 금액 불일치: 주문 {o['total']}원 / 결제 {paid_amount}원. 자동 취소 "
                + ("성공" if cancelled else "실패 — 네이버페이 센터에서 직접 취소 필요"))
        db().execute("UPDATE orders SET status='review', payment_id=?, admin_note=? WHERE id=?",
                     (payment_id, note, o["id"]))
        db().commit()
        return back_to_order(order_no, token)

    mark_paid(o, payment_id, "네이버페이")
    session["cart"] = {}
    return back_to_order(order_no, token)


@app.route("/api/orders/<order_no>/<token>/test-pay", methods=["POST"])
def api_test_pay(order_no, token):
    """네이버페이 키를 넣기 전, 주문 흐름을 확인하기 위한 가짜 결제. 돈은 오가지 않음."""
    if NAVERPAY_ON:
        raise ApiError("테스트 결제는 꺼져 있어요.", 404)
    o = find_order(order_no, token)
    mark_paid(o, "TEST-" + secrets.token_hex(4), "테스트 결제")
    session["cart"] = {}
    return jsonify(ok=True)


# ───────────────────────── 토스 자동충전 (빌링) ─────────────────────────
def digits_of(phone):
    return "".join(ch for ch in str(phone) if ch.isdigit())


def customer_key_of(phone):
    """연락처 → 토스 구매자 ID. 토스에는 원본 연락처를 보내지 않는다."""
    return "shop_" + hashlib.sha256(("ck-salt:" + digits_of(phone)).encode()).hexdigest()[:24]


def toss_auth():
    return "Basic " + __import__("base64").b64encode(
        (TOSS_SECRET_KEY + ":").encode()).decode()


def toss_api(path, data=None, method="POST"):
    """토스페이먼츠 코어 API 호출. (성공여부, 응답 dict) 반환."""
    req = urllib.request.Request(
        "https://api.tosspayments.com" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": toss_auth(), "Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=65) as r:
            return True, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read().decode())
        except Exception:
            res = {"code": "Fail", "message": f"HTTP {e.code}"}
        return False, res
    except Exception as e:
        return False, {"code": "Fail", "message": f"연결 실패: {e}"}


def credit_account(phone):
    """연락처로 충전 계정 찾기. 없으면 만들지 않고 None."""
    return db().execute("SELECT * FROM credit_accounts WHERE phone=?",
                        (digits_of(phone),)).fetchone()


def find_credit(phone, token):
    a = credit_account(phone)
    if not a or not a["token"] or not secrets.compare_digest(a["token"], str(token)):
        raise ApiError("충전 계정을 찾을 수 없어요. 연락처를 다시 확인해 주세요.", 404)
    return a


def credit_json(a, message=None):
    card = db().execute("SELECT billing_key FROM credit_accounts WHERE phone=?",
                        (a["phone"],)).fetchone()
    charges = db().execute(
        "SELECT amount, status, created_at FROM credit_charges WHERE phone=?"
        " ORDER BY id DESC LIMIT 20", (a["phone"],)).fetchall()
    return {"phone": a["phone"], "token": a["token"], "balance": a["balance"],
            "has_card": bool(a["customer_key"] and a["balance"] is not None and card
                             and card["billing_key"]),
            "auto_threshold": a["auto_threshold"], "auto_amount": a["auto_amount"],
            "options": CREDIT_OPTIONS, "message": message,
            "charges": [dict(c) for c in charges]}


def credit_card_register_start():
    """카드 등록창을 띄우기 위한 정보. 연동 규약(모듈 레벨) 참고."""
    if not CREDIT_ON:
        raise ApiError("충전 기능이 아직 연결되지 않았어요.")
    b = body()
    phone = digits_of(b.get("phone", ""))
    if len(phone) < 9:
        raise ApiError("연락처를 정확히 적어 주세요.")
    con = db()
    a = credit_account(phone)
    if not a:
        con.execute("INSERT INTO credit_accounts (phone, token, customer_key, balance,"
                    " created_at) VALUES (?,?,?,?,?)",
                    (phone, secrets.token_urlsafe(16), customer_key_of(phone), 0, now()))
        con.commit()
        a = credit_account(phone)
    return jsonify(client_key=TOSS_CLIENT_KEY, customer_key=a["customer_key"],
                   success_url=f"{site_url()}/pay/toss/card/success/{a['phone']}/{a['token']}",
                   fail_url=f"{site_url()}/pay/toss/card/fail/{a['phone']}/{a['token']}")


# 위 함수를 라우트로 노출
app.add_url_rule("/api/credit/card/start", "credit_card_register_start",
                 credit_card_register_start, methods=["POST"])


@app.route("/pay/toss/card/success/<phone>/<token>")
def toss_card_success(phone, token):
    a = find_credit(phone, token)
    auth_key = request.args.get("authKey", "")
    ck = request.args.get("customerKey", "")
    if not auth_key or ck != a["customer_key"]:
        return redirect(f"/?m=" + urllib.parse.quote("카드 등록을 마치지 못했어요.") +
                        f"#/credit/{a['phone']}/{a['token']}")
    ok, res = toss_api("/v1/billing/authorizations/issue",
                       {"authKey": auth_key, "customerKey": a["customer_key"]})
    if not ok or not res.get("billingKey"):
        msg = "카드 등록에 실패했어요: " + str(res.get("message", ""))
        return redirect(f"/?m=" + urllib.parse.quote(msg) +
                        f"#/credit/{a['phone']}/{a['token']}")
    con = db()
    con.execute("UPDATE credit_accounts SET billing_key_holder=1 WHERE phone=?", (a["phone"],))
    con.execute("CREATE TABLE IF NOT EXISTS _noop (x)")  # placeholder no-op
    con.execute("DELETE FROM _noop")
    con.commit()
    return redirect(f"/?m=" + urllib.parse.quote("카드를 등록했어요. 이제 자동 충전을 쓸 수 있어요.") +
                    f"#/credit/{a['phone']}/{a['token']}")


@app.route("/pay/toss/card/fail/<phone>/<token>")
def toss_card_fail(phone, token):
    a = find_credit(phone, token)
    msg = request.args.get("message", "카드 등록이 취소됐어요.")
    return redirect(f"/?m=" + urllib.parse.quote(msg) + f"#/credit/{a['phone']}/{a['token']}")
</arg_value></tool_call>