"""독립 쇼핑몰 서버 — 화면은 index.html 한 파일, 이 파일은 그 뒤의 API.

실행:  python server.py            (서버 시작)
       python server.py set-admin  (관리자 비밀번호 설정/변경)
"""
import base64
import getpass
import hashlib
import json
import re
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


SHOP_NAME = env("SHOP_NAME", "devkit")
SHOP_TAGLINE = env("SHOP_TAGLINE", "코딩이 빨라지는 도구들")
SHOP_INFO = env("SHOP_INFO", "")  # 푸터 사업자 정보 (상호, 대표, 사업자번호, 통신판매업 신고번호, 연락처)
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

# 토스페이먼츠 충전금 (가상계좌로 이체하면 자동 충전)
TOSS_CLIENT_KEY = env("TOSS_CLIENT_KEY")
TOSS_SECRET_KEY = env("TOSS_SECRET_KEY")
# "1000, 1500 10000,30000" 처럼 쉼표·띄어쓰기가 섞여 있어도 숫자만 골라 읽음
CREDIT_OPTIONS = sorted({int(x) for x in re.findall(r"\d+", env("CREDIT_OPTIONS", "10000,30000,50000"))
                         if 100 <= int(x) <= 2000000})
CREDIT_ON = bool(TOSS_CLIENT_KEY and TOSS_SECRET_KEY and CREDIT_OPTIONS)
# 가짜(테스트) 결제 버튼은 결제 수단이 하나도 연결되지 않았을 때만 나온다
TESTPAY_ON = not NAVERPAY_ON and not CREDIT_ON

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
CREATE TABLE IF NOT EXISTS wallets (
  phone TEXT PRIMARY KEY, pw_hash TEXT, customer_key TEXT, balance INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS wallet_tx (
  id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, kind TEXT, amount INTEGER, ref TEXT UNIQUE,
  payment_key TEXT DEFAULT '', secret TEXT DEFAULT '', status TEXT, note TEXT DEFAULT '', created_at TEXT);
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
    if request.method == "POST" and request.path != "/pay/toss/webhook":
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
                   credit=CREDIT_ON, credit_user=bool(session.get("wallet")), testpay=TESTPAY_ON,
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
    if not TESTPAY_ON:
        raise ApiError("테스트 결제는 꺼져 있어요.", 404)
    o = find_order(order_no, token)
    mark_paid(o, "TEST-" + secrets.token_hex(4), "테스트 결제")
    session["cart"] = {}
    return jsonify(ok=True)


# ───────────────────────── 충전금 (토스페이먼츠 가상계좌 자동충전) ─────────────────────────
# 흐름: 충전 계정(연락처+비밀번호) → 금액 선택 → 토스가 입금 전용 가상계좌 발급 → 손님이 계좌이체
#       → 토스가 입금을 알려 줌(웹훅) → 충전금이 자동으로 올라감 → 충전금으로 주문 결제.
# 웹훅 내용은 그대로 믿지 않고, 시크릿 키로 토스에 다시 물어본 결과만 반영한다.
BANKS = {"39": "경남", "34": "광주", "12": "단위농협", "32": "부산", "45": "새마을금고", "64": "산림조합",
         "88": "신한", "48": "신협", "27": "씨티", "20": "우리", "71": "우체국", "50": "저축은행",
         "37": "전북", "35": "제주", "90": "카카오뱅크", "89": "케이뱅크", "92": "토스뱅크", "81": "하나",
         "54": "HSBC", "03": "IBK기업", "06": "KB국민", "04": "KB국민", "31": "iM뱅크", "02": "산업",
         "11": "NH농협", "23": "SC제일", "07": "수협"}


def digits_of(phone):
    return "".join(ch for ch in str(phone) if ch.isdigit())


def customer_key_of(phone):
    """연락처 → 토스 구매자 ID. 토스에는 원본 연락처를 보내지 않는다."""
    return "shop_" + hashlib.sha256((app.secret_key + ":" + phone).encode()).hexdigest()[:32]


def toss_api(path, data=None):
    """토스페이먼츠 API 호출 (data 가 있으면 POST, 없으면 GET). (성공여부, 응답 dict) 반환."""
    headers = {"Authorization": "Basic " + base64.b64encode((TOSS_SECRET_KEY + ":").encode()).decode(),
               "Content-Type": "application/json"}
    req = urllib.request.Request("https://api.tosspayments.com" + path,
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=65) as r:
            return True, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode())
        except Exception:
            return False, {"message": f"HTTP {e.code}"}
    except Exception as e:  # 네트워크 오류, 시간 초과
        return False, {"message": f"결제 서버에 연결하지 못했어요 ({e})", "unreachable": True}


def wallet_row(phone):
    return db().execute("SELECT * FROM wallets WHERE phone=?", (phone,)).fetchone()


def current_wallet():
    if not CREDIT_ON:
        raise ApiError("충전 기능이 아직 연결되지 않았어요.", 404)
    a = wallet_row(session.get("wallet", ""))
    if not a:
        session.pop("wallet", None)
        raise ApiError("충전 계정에 로그인해 주세요.", 401)
    return a


def wallet_json(a, message=None):
    tx = db().execute("SELECT kind, amount, status, note, ref, created_at FROM wallet_tx"
                      " WHERE phone=? AND status<>'ready' ORDER BY id DESC LIMIT 30",
                      (a["phone"],)).fetchall()
    return {"on": True, "logged_in": True, "phone": a["phone"], "balance": a["balance"],
            "options": CREDIT_OPTIONS, "message": message, "history": [dict(t) for t in tx]}


def account_note(pay):
    """토스 결제 정보 → '신한 123456789 (예금주 …, 11-21 14:00까지)'"""
    va = pay.get("virtualAccount") or {}
    bank = BANKS.get(str(va.get("bankCode", "")), str(va.get("bankCode", "")))
    due = str(va.get("dueDate", ""))[5:16].replace("T", " ")
    return (f"{bank} {va.get('accountNumber', '')} (예금주 {va.get('customerName', '')}"
            + (f", {due}까지" if due else "") + ")")


def settle_charge(ref):
    """입금 대기 중인 충전 건을 토스에 다시 확인해, 입금이 끝났으면 충전금에 더한다. 새 상태를 반환."""
    con = db()
    t = con.execute("SELECT * FROM wallet_tx WHERE ref=? AND kind='charge'", (ref,)).fetchone()
    if not t or t["status"] != "waiting":
        return t["status"] if t else None
    ok, pay = toss_api("/v1/payments/orders/" + urllib.parse.quote(ref, safe=""))
    if not ok:
        return "waiting"
    status = pay.get("status")
    if status == "DONE" and int(pay.get("totalAmount") or 0) == t["amount"]:
        # waiting → done 은 딱 한 번만 일어난다 (웹훅과 '입금 확인'이 겹쳐도 중복 충전 없음)
        cur = con.execute("UPDATE wallet_tx SET status='done' WHERE ref=? AND status='waiting'", (ref,))
        if cur.rowcount:
            con.execute("UPDATE wallets SET balance=balance+? WHERE phone=?", (t["amount"], t["phone"]))
        con.commit()
        return "done"
    if status in ("CANCELED", "EXPIRED", "ABORTED", "PARTIAL_CANCELED"):
        con.execute("UPDATE wallet_tx SET status='failed' WHERE ref=? AND status='waiting'", (ref,))
        con.commit()
        return "failed"
    return "waiting"


_wallet_fails = {}


@app.route("/api/credit")
def api_credit():
    if not CREDIT_ON:
        return jsonify(on=False)
    a = wallet_row(session.get("wallet", ""))
    return jsonify(wallet_json(a)) if a else jsonify(on=True, logged_in=False)


@app.route("/api/credit/enter", methods=["POST"])
def api_credit_enter():
    """mode=signup: 새 충전 계정 / mode=login: 로그인."""
    if not CREDIT_ON:
        raise ApiError("충전 기능이 아직 연결되지 않았어요.", 404)
    b = body()
    phone, pw = digits_of(b.get("phone", "")), str(b.get("password", ""))
    if len(phone) < 9 or len(phone) > 15:
        raise ApiError("연락처를 정확히 적어 주세요.")
    key = (request.remote_addr or "?") + ":" + phone
    recent = [t for t in _wallet_fails.get(key, []) if time.time() - t < 600]
    if len(recent) >= 5:
        raise ApiError("시도가 너무 많아요. 10분 뒤에 다시 해 주세요.", 429)
    a = wallet_row(phone)
    if b.get("mode") == "signup":
        if a:
            raise ApiError("이미 충전 계정이 있는 연락처예요. 로그인해 주세요.")
        if len(pw) < 6:
            raise ApiError("비밀번호는 6자 이상으로 정해 주세요.")
        db().execute("INSERT INTO wallets (phone, pw_hash, customer_key, created_at) VALUES (?,?,?,?)",
                     (phone, generate_password_hash(pw), customer_key_of(phone), now()))
        db().commit()
    elif not a or not check_password_hash(a["pw_hash"], pw):
        _wallet_fails[key] = recent + [time.time()]
        raise ApiError("연락처 또는 비밀번호가 맞지 않아요.", 401)
    _wallet_fails.pop(key, None)
    session["wallet"] = phone
    return jsonify(wallet_json(wallet_row(phone)))


@app.route("/api/credit/logout", methods=["POST"])
def api_credit_logout():
    session.pop("wallet", None)
    return jsonify(ok=True)


@app.route("/api/credit/charge/start", methods=["POST"])
def api_credit_charge_start():
    """충전 요청을 기록하고, 토스 결제창(가상계좌)을 띄우는 데 필요한 값을 돌려준다."""
    a = current_wallet()
    try:
        amount = int(body().get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    if amount not in CREDIT_OPTIONS:
        raise ApiError("충전할 금액을 골라 주세요.")
    ref = "C" + datetime.now().strftime("%y%m%d") + "-" + secrets.token_hex(6).upper()
    db().execute("INSERT INTO wallet_tx (phone, kind, amount, ref, status, created_at)"
                 " VALUES (?,?,?,?,?,?)", (a["phone"], "charge", amount, ref, "ready", now()))
    db().commit()
    return jsonify(client_key=TOSS_CLIENT_KEY, customer_key=a["customer_key"], order_id=ref,
                   order_name=f"{SHOP_NAME} 충전금 {amount:,}원", amount=amount,
                   success_url=f"{site_url()}/pay/toss/charge/success",
                   fail_url=f"{site_url()}/pay/toss/charge/fail")


def back_to_credit(message):
    return redirect("/?m=" + urllib.parse.quote(message) + "#/credit")


@app.route("/pay/toss/charge/success")
def toss_charge_success():
    """결제창에서 가상계좌 발급을 마치고 돌아오는 곳. 금액을 확인하고 승인하면 계좌번호가 확정된다."""
    ref, payment_key = request.args.get("orderId", ""), request.args.get("paymentKey", "")
    con = db()
    t = con.execute("SELECT * FROM wallet_tx WHERE ref=? AND kind='charge'", (ref,)).fetchone()
    if not CREDIT_ON or not t or not payment_key:
        return back_to_credit("충전 요청을 찾을 수 없어요. 다시 시도해 주세요.")
    if t["status"] != "ready":
        return back_to_credit("")
    if request.args.get("amount", "") != str(t["amount"]):
        con.execute("UPDATE wallet_tx SET status='failed', note='금액 불일치' WHERE ref=?", (ref,))
        con.commit()
        return back_to_credit("충전 금액이 요청과 달라 취소했어요. 다시 시도해 주세요.")
    ok, pay = toss_api("/v1/payments/confirm",
                       {"paymentKey": payment_key, "orderId": ref, "amount": t["amount"]})
    if not ok:
        con.execute("UPDATE wallet_tx SET status='failed', note=? WHERE ref=?",
                    (str(pay.get("message", ""))[:200], ref))
        con.commit()
        return back_to_credit("계좌를 발급하지 못했어요: " + str(pay.get("message", "")))
    con.execute("UPDATE wallet_tx SET status='waiting', payment_key=?, secret=?, note=? WHERE ref=?"
                " AND status='ready'", (payment_key, str(pay.get("secret") or ""), account_note(pay), ref))
    con.commit()
    if settle_charge(ref) == "done":
        return back_to_credit(f"{t['amount']:,}원을 충전했어요.")
    return back_to_credit("입금 계좌를 발급했어요. 아래 계좌로 이체하면 자동으로 충전돼요.")


@app.route("/pay/toss/charge/fail")
def toss_charge_fail():
    ref = request.args.get("orderId", "")
    if ref:
        db().execute("UPDATE wallet_tx SET status='failed' WHERE ref=? AND status='ready'", (ref,))
        db().commit()
    return back_to_credit(request.args.get("message") or "충전을 취소했어요.")


@app.route("/pay/toss/webhook", methods=["POST"])
def toss_webhook():
    """토스가 입금을 알려 주는 주소 (개발자센터 → 웹훅에 등록). 받은 주문번호를 토스에 다시 확인한다."""
    b = body()
    ref = str(b.get("orderId") or (b.get("data") or {}).get("orderId") or "")
    if CREDIT_ON and ref:
        settle_charge(ref)
    return jsonify(ok=True)


@app.route("/api/credit/refresh", methods=["POST"])
def api_credit_refresh():
    """'입금 확인' 버튼: 내 입금 대기 건들을 토스에 다시 확인."""
    a = current_wallet()
    waiting = db().execute("SELECT ref FROM wallet_tx WHERE phone=? AND kind='charge' AND"
                           " status='waiting' ORDER BY id DESC LIMIT 5", (a["phone"],)).fetchall()
    done = sum(1 for t in waiting if settle_charge(t["ref"]) == "done")
    msg = "입금을 확인해 충전했어요." if done else ("아직 입금이 확인되지 않았어요." if waiting else None)
    return jsonify(wallet_json(wallet_row(a["phone"]), msg))


@app.route("/api/orders/<order_no>/<token>/credit-pay", methods=["POST"])
def api_credit_pay(order_no, token):
    """충전금으로 주문 결제."""
    a = current_wallet()
    o = find_order(order_no, token)
    if o["status"] != "pending":
        raise ApiError("이미 처리된 주문이에요.")
    phone, total, con = a["phone"], o["total"], db()
    if a["balance"] < total:
        raise ApiError(f"충전금이 {total - a['balance']:,}원 모자라요. 충전한 뒤 다시 결제해 주세요.", 402)
    ref = "U-" + o["order_no"]
    cur = con.execute("UPDATE wallets SET balance=balance-? WHERE phone=? AND balance>=?",
                      (total, phone, total))
    con.commit()
    if cur.rowcount == 0:
        raise ApiError("충전금이 모자라요. 다시 시도해 주세요.", 402)
    if not mark_paid(o, ref, "충전금"):
        con.execute("UPDATE wallets SET balance=balance+? WHERE phone=?", (total, phone))
        con.commit()
        raise ApiError("이미 처리된 주문이에요.")
    con.execute("INSERT OR IGNORE INTO wallet_tx (phone, kind, amount, ref, status, note, created_at)"
                " VALUES (?,?,?,?,?,?,?)", (phone, "use", -total, ref, "done", "주문 " + o["order_no"], now()))
    con.execute("UPDATE orders SET admin_note=? WHERE id=?", ("충전 계정 " + phone, o["id"]))
    con.commit()
    session["cart"] = {}
    return jsonify(ok=True)


def refund_credit(o):
    """충전금으로 결제한 주문을 취소하면 충전금으로 되돌려 준다."""
    use = db().execute("SELECT * FROM wallet_tx WHERE ref=? AND kind='use'",
                       ("U-" + o["order_no"],)).fetchone()
    if not use:
        return False
    cur = db().execute("INSERT OR IGNORE INTO wallet_tx (phone, kind, amount, ref, status, note,"
                       " created_at) VALUES (?,?,?,?,?,?,?)",
                       (use["phone"], "refund", o["total"], "R-" + o["order_no"], "done",
                        "주문 취소 " + o["order_no"], now()))
    if cur.rowcount:
        db().execute("UPDATE wallets SET balance=balance+? WHERE phone=?", (o["total"], use["phone"]))
    return bool(cur.rowcount)


# ───────────────────────── 관리자 API (나만) ─────────────────────────
_fails = {}  # ip → 실패 시각 목록


@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    row = db().execute("SELECT value FROM settings WHERE key='admin_hash'").fetchone()
    if not row:
        raise ApiError("아직 관리자 비밀번호가 없어요. 서버에서 python server.py set-admin 을 실행해 주세요.")
    ip = request.remote_addr or "?"
    recent = [t for t in _fails.get(ip, []) if time.time() - t < 600]
    if len(recent) >= 5:
        raise ApiError("로그인 시도가 너무 많아요. 10분 뒤에 다시 해 주세요.", 429)
    if not check_password_hash(row["value"], str(body().get("password", ""))):
        _fails[ip] = recent + [time.time()]
        raise ApiError("비밀번호가 맞지 않아요.", 401)
    _fails.pop(ip, None)
    session["admin"] = True
    session.permanent = True
    return jsonify(csrf=csrf_token())


@app.route("/api/admin/logout", methods=["POST"])
def api_admin_logout():
    session.pop("admin", None)
    return jsonify(csrf=csrf_token())


@app.route("/api/admin/orders")
@admin_required
def api_admin_orders():
    tab = request.args.get("s", "paid")
    if tab not in STATUS and tab != "all":
        tab = "paid"
    q = "SELECT * FROM orders" + ("" if tab == "all" else " WHERE status=?") + " ORDER BY id DESC LIMIT 300"
    out = []
    for o in db().execute(q, () if tab == "all" else (tab,)).fetchall():
        its = order_items(o["id"])
        summary = (its[0]["name"] + (f" 외 {len(its) - 1}건" if len(its) > 1 else "")) if its else ""
        out.append({"id": o["id"], "order_no": o["order_no"], "name": o["name"], "total": o["total"],
                    "status": o["status"], "created_at": o["created_at"], "summary": summary})
    counts = {r["status"]: r["n"] for r in db().execute(
        "SELECT status, COUNT(*) n FROM orders GROUP BY status")}
    return jsonify(orders=out, counts=counts, tab=tab, labels=STATUS)


def admin_order_json(o):
    d = {k: o[k] for k in o.keys() if k != "token"}
    d["status_label"] = STATUS.get(o["status"], o["status"])
    d["items"] = [dict(i) for i in order_items(o["id"])]
    return d


@app.route("/api/admin/orders/<int:oid>", methods=["GET", "POST"])
@admin_required
def api_admin_order(oid):
    con = db()
    o = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("주문을 찾을 수 없어요.", 404)
    message = None
    if request.method == "POST":
        b = body()
        action = b.get("action")
        if action == "purchased" and o["status"] == "paid":
            con.execute("UPDATE orders SET status='purchased' WHERE id=?", (oid,))
        elif action == "ship" and o["status"] in ("paid", "purchased", "shipped"):
            tracking = str(b.get("tracking", "")).strip()[:40]
            if not tracking:
                raise ApiError("운송장 번호를 적어 주세요.")
            con.execute("UPDATE orders SET status='shipped', courier=?, tracking=? WHERE id=?",
                        (str(b.get("courier", "")).strip()[:30], tracking, oid))
        elif action == "done" and o["status"] == "shipped":
            con.execute("UPDATE orders SET status='done' WHERE id=?", (oid,))
        elif action == "cancel" and o["status"] != "canceled":
            if o["status"] in ("paid", "purchased"):  # 차감했던 재고 되돌리기
                for i in order_items(oid):
                    con.execute("UPDATE products SET stock=stock+? WHERE id=?",
                                (i["qty"], i["product_id"]))
            con.execute("UPDATE orders SET status='canceled' WHERE id=?", (oid,))
            if o["pay_method"] == "충전금" and refund_credit(o):
                message = "주문을 취소하고 결제 금액을 손님 충전금으로 되돌렸어요."
            else:
                message = "주문을 취소로 표시했어요. 결제 환불은 네이버페이 센터에서 직접 진행해 주세요."
        elif action == "note":
            con.execute("UPDATE orders SET admin_note=? WHERE id=?",
                        (str(b.get("admin_note", ""))[:1000], oid))
            message = "메모를 저장했어요."
        con.commit()
        o = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    return jsonify(order=admin_order_json(o), message=message)


@app.route("/api/admin/products")
@admin_required
def api_admin_products():
    rows = db().execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    return jsonify([product_json(p, admin=True) for p in rows])


@app.route("/api/admin/products/<int:pid>")
@admin_required
def api_admin_product(pid):
    p = db().execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        raise ApiError("상품을 찾을 수 없어요.", 404)
    return jsonify(product_json(p, admin=True))


def save_image(file, notes):
    ext = os.path.splitext(file.filename)[1].lower()
    head = file.stream.read(12)
    file.stream.seek(0)
    is_image = (head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8")
                or head.startswith(b"GIF8") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))
    if ext not in ALLOWED_IMAGE or not is_image:
        notes.append(f"‘{file.filename}’은(는) 올릴 수 없는 파일이에요 (png, jpg, webp, gif 사진만).")
        return None
    name = uuid.uuid4().hex + ext
    file.save(os.path.join(UPLOAD_DIR, name))
    return name


def remove_upload(name):
    path = os.path.join(UPLOAD_DIR, os.path.basename(name))
    if os.path.isfile(path):
        os.remove(path)


def update_images(con, pid, form, files, notes):
    """사진 삭제 → 새 사진 추가 → 대표 사진 지정 → products.image 를 대표 사진으로 맞춤."""
    for iid in form.getlist("delete_image"):
        row = con.execute("SELECT * FROM product_images WHERE id=? AND product_id=?",
                          (iid, pid)).fetchone()
        if row:
            con.execute("DELETE FROM product_images WHERE id=?", (row["id"],))
            remove_upload(row["file"])
    count = con.execute("SELECT COUNT(*) n FROM product_images WHERE product_id=?",
                        (pid,)).fetchone()["n"]
    for f in files:
        if not f or not f.filename:
            continue
        if count >= MAX_IMAGES:
            notes.append(f"사진은 상품마다 {MAX_IMAGES}장까지예요. 나머지는 올리지 않았어요.")
            break
        name = save_image(f, notes)
        if name:
            con.execute("INSERT INTO product_images (product_id, file, pos) VALUES (?,?,?)",
                        (pid, name, count + 1))
            count += 1
    main = form.get("main_image", "")
    if main:
        con.execute("UPDATE product_images SET pos=pos+1 WHERE product_id=?", (pid,))
        con.execute("UPDATE product_images SET pos=0 WHERE id=? AND product_id=?", (main, pid))
    first = con.execute("SELECT file FROM product_images WHERE product_id=? ORDER BY pos, id"
                        " LIMIT 1", (pid,)).fetchone()
    con.execute("UPDATE products SET image=? WHERE id=?", (first["file"] if first else "", pid))


@app.route("/api/admin/products", methods=["POST"])
@admin_required
def api_admin_product_save():
    """상품 추가(id 없음) 또는 수정(id 있음). 사진 때문에 multipart 폼으로 받음."""
    con, f = db(), request.form
    pid = f.get("id", type=int)
    p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone() if pid else None
    if pid and not p:
        raise ApiError("상품을 찾을 수 없어요.", 404)
    name = f.get("name", "").strip()[:80]
    try:
        price = int(f.get("price", "0").replace(",", ""))
        stock = int(f.get("stock", "0"))
    except ValueError:
        price, stock = -1, -1
    source_url = f.get("source_url", "").strip()[:500]
    if not name or price < 0 or stock < 0:
        raise ApiError("상품 이름, 가격, 재고를 확인해 주세요.")
    if source_url and not source_url.startswith(("http://", "https://")):
        raise ApiError("구매처 링크는 http:// 또는 https:// 로 시작해야 해요.")
    vals = (name, price, f.get("description", "").strip()[:5000], stock,
            1 if f.get("active") else 0, source_url, f.get("source_note", "").strip()[:300])
    if p:
        con.execute("UPDATE products SET name=?, price=?, description=?, stock=?, active=?,"
                    " source_url=?, source_note=? WHERE id=?", vals + (pid,))
    else:
        pid = con.execute("INSERT INTO products (name, price, description, stock, active,"
                          " source_url, source_note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                          vals + (now(),)).lastrowid
    notes = []
    update_images(con, pid, f, request.files.getlist("images"), notes)
    con.commit()
    return jsonify(id=pid, message=" ".join(["상품을 저장했어요."] + notes))


@app.route("/api/admin/products/<int:pid>/delete", methods=["POST"])
@admin_required
def api_admin_product_delete(pid):
    con = db()
    for row in images_of(pid):
        remove_upload(row["file"])
    con.execute("DELETE FROM product_images WHERE product_id=?", (pid,))
    con.execute("DELETE FROM products WHERE id=?", (pid,))
    con.commit()
    return jsonify(ok=True)


# ───────────────────────── 실행 ─────────────────────────
def set_admin():
    init_db()
    pw = getpass.getpass("새 관리자 비밀번호 (8자 이상): ")
    if len(pw) < 8:
        sys.exit("8자 이상으로 정해 주세요.")
    if pw != getpass.getpass("한 번 더 입력: "):
        sys.exit("두 번 입력한 비밀번호가 달라요.")
    con = connect()
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_hash', ?)",
                (generate_password_hash(pw),))
    con.commit()
    con.close()
    print("관리자 비밀번호를 저장했어요. 사이트 주소 뒤에 #/admin 을 붙여 로그인하세요.")


init_db()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "set-admin":
        set_admin()
    else:
        port = env_int("PORT", 8780)
        print(f"상점 서버 시작: http://0.0.0.0:{port}  (네이버페이 {'연결됨 · ' + NP_MODE if NAVERPAY_ON else '미설정 → 테스트 결제 모드'}"
              f" / 토스 충전금 {('켜짐 · ' + ('테스트 키' if TOSS_SECRET_KEY.startswith('test_') else '실제 키')) if CREDIT_ON else '꺼짐'})")
        app.run(host="0.0.0.0", port=port, threaded=True)
