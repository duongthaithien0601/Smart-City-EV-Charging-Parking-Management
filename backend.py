"""
SMART CITY EV – BACKEND FLASK
"""

import os
import hashlib
import math
import secrets
import time
import uuid
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, jsonify, request, session, send_file
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "smart_city_emerald_secret_2026")

# Lịch đặt chỗ dùng giờ Việt Nam; lưu mốc thời gian có múi giờ trong PostgreSQL.
APP_TIMEZONE = timezone(timedelta(hours=7), name="ICT")
def parse_local_datetime(value: str) -> datetime:
    """Đọc giờ từ biểu mẫu và trả về datetime có múi giờ Việt Nam."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=APP_TIMEZONE)
    return parsed.astimezone(APP_TIMEZONE)


def auto_finish_expired_charging_sessions():
    """Close active charging sessions once the booking and 15-minute grace period end."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ss.session_id, ss.start_date_time, ss.end_date_time, ss.session_status,
                           ss.charging_station_id, st.slot_id, cs.kw_rate, r.end_time
                    FROM charging_sessions ss
                    JOIN reservations r ON r.reservation_id=ss.reservation_id
                    JOIN charging_stations st ON st.station_id=ss.charging_station_id
                    JOIN charging_slots cs ON cs.slot_id=st.slot_id
                    WHERE (ss.session_status='Charging'
                           AND r.end_time + INTERVAL '15 minutes' <= %s)
                       OR (ss.session_status='Completed' AND r.payment_status='Pending'
                           AND ss.vehicle_departed_at IS NULL
                           AND ss.end_date_time > r.end_time + INTERVAL '15 minutes')
                    FOR UPDATE OF ss, r, st;
                """, (datetime.now(APP_TIMEZONE),))
                expired = cur.fetchall()
                for charging in expired:
                    cutoff = charging["end_time"] + timedelta(minutes=15)
                    duration_hours = max(0.0, (cutoff - charging["start_date_time"]).total_seconds() / 3600.0)
                    energy_kwh = round(max(0.5, duration_hours * float(charging["kw_rate"]) * 0.85), 2)
                    cur.execute("""
                        UPDATE charging_sessions
                        SET end_date_time=%s, energy_consumed_kwh=%s, session_status='Completed'
                        WHERE session_id=%s AND session_status IN ('Charging', 'Completed');
                    """, (cutoff, energy_kwh, charging["session_id"]))
                    cur.execute("UPDATE charging_stations SET status='Online' WHERE station_id=%s AND status='Busy';", (charging["charging_station_id"],))
            conn.commit()
    except psycopg2.Error as error:
        app.logger.error("Automatic charging-session completion failed: %s", error)


def charging_session_watchdog():
    while True:
        auto_finish_expired_charging_sessions()
        time.sleep(5)

# ==============================================================================
# TIỆN ÍCH, DATABASE VÀ TÁC VỤ NỀN
# ==============================================================================
# CẤU HÌNH KẾT NỐI POSTGRESQL
# ==============================================================================
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "dbname": os.environ.get("DB_NAME", "smart_city"),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "123456"),
}


def get_db_connection():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


def hash_text(raw_text: str) -> str:
    """Mã hóa mật khẩu và dữ liệu thanh toán bằng SHA-256 khi lưu xuống CSDL."""
    return hashlib.sha256(str(raw_text).encode("utf-8")).hexdigest()


def generate_db_id(cur, entity: str) -> str:
    """Sinh mã tuần tự cho các bản ghi cấu hình do quản trị viên tạo."""
    generators = {
        "location": ("parking_locations", "location_id", "LOC"),
        "slot": ("slots", "slot_id", "SLT"),
        "station": ("charging_stations", "station_id", "STN"),
        "parking_rate": ("parking_rates", "rate_id", "PR"),
        "charging_rate": ("charging_rates", "rate_id", "CR"),
        "violation_rate": ("violation_rates", "rate_id", "VR"),
    }
    if entity not in generators:
        raise ValueError("Loại mã không hợp lệ.")
    table, column, prefix = generators[entity]
    cur.execute(f"LOCK TABLE {table} IN EXCLUSIVE MODE;")
    cur.execute(f"""
        SELECT COALESCE(MAX(SUBSTRING({column} FROM {len(prefix) + 1})::INTEGER), 0) + 1 AS next_id
        FROM {table}
        WHERE {column} ~ %s;
    """, (f"^{prefix}[0-9]+$",))
    return f"{prefix}{cur.fetchone()['next_id']:06d}"


def generate_reservation_id(cur) -> str:
    """Create a unique random reservation code containing exactly nine digits."""
    cur.execute("LOCK TABLE reservations IN SHARE ROW EXCLUSIVE MODE;")
    for _ in range(100):
        reservation_id = str(secrets.randbelow(900_000_000) + 100_000_000)
        cur.execute("SELECT 1 FROM reservations WHERE reservation_id = %s;", (reservation_id,))
        if not cur.fetchone():
            return reservation_id
    raise RuntimeError("Không thể tạo mã đặt chỗ duy nhất. Vui lòng thử lại.")


def slot_lock_reason(cur, slot_id: str):
    cur.execute("SELECT status FROM slots WHERE slot_id=%s;", (slot_id,))
    slot = cur.fetchone()
    if not slot:
        return "Không tìm thấy vị trí."
    if slot["status"] == "Occupied":
        return "Vị trí đang có xe."
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM reservations
            WHERE slot_id=%s AND payment_status IN ('Pending', 'Paid')
              AND end_time > NOW() AND vehicle_departed_at IS NULL
        ) OR EXISTS (
            SELECT 1 FROM charging_sessions ss
            JOIN charging_stations st ON st.station_id=ss.charging_station_id
            WHERE st.slot_id=%s AND ss.session_status='Charging'
        ) AS is_locked;
    """, (slot_id, slot_id))
    return "Vị trí đang có lịch đặt hoặc phiên sạc." if cur.fetchone()["is_locked"] else None


def location_lock_reason(cur, location_id: str):
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM slots s
            WHERE s.location_id=%s AND s.status='Occupied'
        ) AS has_occupied_slot,
        EXISTS (
            SELECT 1 FROM reservations r JOIN slots s ON s.slot_id=r.slot_id
            WHERE s.location_id=%s AND r.payment_status IN ('Pending', 'Paid')
              AND r.end_time > NOW() AND r.vehicle_departed_at IS NULL
        ) AS has_active_reservation,
        EXISTS (
            SELECT 1 FROM charging_sessions ss
            JOIN charging_stations st ON st.station_id=ss.charging_station_id
            JOIN slots s ON s.slot_id=st.slot_id
            WHERE s.location_id=%s AND ss.session_status='Charging'
        ) AS has_active_session;
    """, (location_id, location_id, location_id))
    state = cur.fetchone()
    if state["has_occupied_slot"]:
        return "Bãi có vị trí đang có xe."
    if state["has_active_reservation"]:
        return "Bãi có vị trí đang có lịch đặt chưa kết thúc."
    if state["has_active_session"]:
        return "Bãi có phiên sạc đang hoạt động."
    return None


def locked_location_ids(cur):
    cur.execute("SELECT location_id FROM parking_locations ORDER BY location_id;")
    location_ids = [row["location_id"] for row in cur.fetchall()]
    return {
        location_id: reason
        for location_id in location_ids
        if (reason := location_lock_reason(cur, location_id))
    }


def read_number(value, label: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} phải là số hợp lệ.")
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        qualifier = "không được âm" if allow_zero else "phải lớn hơn 0"
        raise ValueError(f"{label} {qualifier}.")
    return number


def is_valid_gmail(email_str: str) -> bool:
    """Kiểm tra email bắt buộc có đuôi @gmail.com."""
    e = (email_str or "").strip().lower()
    return len(e) > 10 and e.endswith("@gmail.com") and " " not in e


def calculate_dynamic_parking_fee(start_dt: datetime, end_dt: datetime, rates_list: list) -> float:
    """Tính phí qua mọi khung giá giao nhau; chỉ từ chối khi có đoạn chưa được cấu hình."""
    total_fee = 0.0
    cur = start_dt
    while cur < end_dt:
        rate = rate_for_time(rates_list, cur.time().replace(tzinfo=None), "price_per_hour")
        if not rate:
            raise ValueError("Chưa có biểu giá đỗ xe bao phủ toàn bộ khoảng thời gian đã chọn.")
        nxt = end_dt
        for candidate in rates_list:
            for boundary in (candidate["effective_from"], candidate["effective_to"]):
                boundary_dt = datetime.combine(cur.date(), boundary).replace(tzinfo=APP_TIMEZONE)
                if boundary_dt <= cur:
                    boundary_dt += timedelta(days=1)
                if boundary_dt < nxt:
                    nxt = boundary_dt
        if nxt <= cur:
            raise ValueError("Không thể xác định ranh giới biểu giá cho khoảng thời gian đã chọn.")

        hours = (nxt - cur).total_seconds() / 3600.0
        tier_price = float(rate["price_per_hour"])
        total_fee += hours * tier_price
        cur = nxt
    return round(total_fee, 0)


def rate_for_time(rates_list: list, at_time, value_column: str = "rate_per_kwh"):
    matches = []
    for rate in rates_list:
        start = rate["effective_from"]
        end = rate["effective_to"]
        covers = start <= at_time < end if start < end else (at_time >= start or at_time < end)
        if covers:
            matches.append(rate)
    return max(matches, key=lambda item: item["rate_id"]) if matches else None


def latest_rates(cur, table: str, location_id=None, service_type=None):
    if table not in ("parking_rates", "charging_rates"):
        raise ValueError("Bảng giá không hợp lệ.")
    if table == "parking_rates":
        cur.execute(f"""
            SELECT DISTINCT ON (rate_type) * FROM {table}
            WHERE location_id = %s AND service_type = %s
            ORDER BY rate_type,
                     COALESCE(NULLIF(substring(rate_id from '[0-9]+$'), ''), '0')::BIGINT DESC;
        """, (location_id, service_type or "parking"))
        return cur.fetchall()
    cur.execute(f"""
        SELECT DISTINCT ON (rate_type) * FROM {table}
        WHERE location_id = %s
        ORDER BY rate_type,
                 COALESCE(NULLIF(substring(rate_id from '[0-9]+$'), ''), '0')::BIGINT DESC;
    """, (location_id,))
    return cur.fetchall()


def intervals_overlap(start_a, end_a, start_b, end_b) -> bool:
    def split(start, end):
        midnight = datetime.min.time()
        if start < end:
            return [(start, end)]
        return [(start, datetime.max.time()), (midnight, end)]

    return any(a0 < b1 and b0 < a1 for a0, a1 in split(start_a, end_a) for b0, b1 in split(start_b, end_b))


def parse_rate_payload(data):
    kind = data.get("kind")
    rate_type = data.get("rate_type")
    if kind not in ("parking", "charging") or rate_type not in ("Peak", "Normal", "Off-Peak"):
        raise ValueError("Loại giá hoặc khung giờ không hợp lệ.")
    try:
        start = datetime.strptime(data.get("effective_from", ""), "%H:%M").time()
        end = datetime.strptime(data.get("effective_to", ""), "%H:%M").time()
    except (TypeError, ValueError):
        raise ValueError("Vui lòng nhập giờ bắt đầu và kết thúc hợp lệ.")
    if start == end:
        raise ValueError("Giờ kết thúc phải khác giờ bắt đầu.")
    column = "price_per_hour" if kind == "parking" else "rate_per_kwh"
    price = read_number(data.get("value"), "Đơn giá")
    return kind, rate_type, start, end, column, price


def ensure_rate_interval_available(cur, kind, rate_type, start, end, location_id, service_type=None, exclude_rate_ids=()):
    table = "parking_rates" if kind == "parking" else "charging_rates"
    for existing in latest_rates(cur, table, location_id, service_type):
        if existing["rate_id"] in exclude_rate_ids:
            continue
        if existing["rate_type"] != rate_type and intervals_overlap(start, end, existing["effective_from"], existing["effective_to"]):
            raise ValueError(f"Khung giờ bị trùng với mức giá {existing['rate_type']} ({existing['effective_from']}–{existing['effective_to']}).")


# ==============================================================================
# MIDDLEWARE KIỂM TRA ĐĂNG NHẬP & PHÂN QUYỀN 2 VAI TRÒ
# ==============================================================================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"status": "error", "message": "Vui lòng đăng nhập để sử dụng hệ thống."}), 401
        return f(*args, **kwargs)
    return decorated


def customer_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if session["user"].get("role") != "customer":
            return jsonify({"status": "error", "message": "Chức năng này chỉ dành cho tài khoản khách hàng."}), 403
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"status": "error", "message": "Vui lòng đăng nhập để tiếp tục."}), 401
        if session["user"].get("role") != "system_admin":
            return jsonify({"status": "error", "message": "Chức năng này chỉ dành cho Quản trị hệ thống."}), 403
        return f(*args, **kwargs)
    return decorated


def translate_pg_error(err: psycopg2.Error) -> str:
    """Chỉ trả về thông báo ngắn gọn bằng tiếng Việt, không lộ mã lỗi SQL hay mã nguồn."""
    msg = (err.pgerror or str(err)).lower()
    if "chk_users_email_gmail" in msg or "chk_users_email" in msg:
        return "Email phải có đuôi @gmail.com"
    if "chk_reservation_time" in msg or "chk_session_time" in msg:
        return "Thời gian kết thúc phải lớn hơn thời gian bắt đầu."
    if "plate_number" in msg or "vehicles_plate_number_key" in msg:
        return "Biển số xe này đã tồn tại trong hệ thống."
    if "users_email_key" in msg or ("email" in msg and "unique" in msg):
        return "Email này đã được đăng ký."
    if "users_phone_number_key" in msg:
        return "Số điện thoại này đã được sử dụng."
    if "chk_vehicle_type" in msg:
        return "Loại phương tiện chỉ nhận Xe điện (EV) hoặc Xe xăng (ICE)."
    if "overlap_slot" in msg or "br25" in msg or "overlapping" in msg or "trùng" in msg:
        return "Chỗ đỗ này đã có người đặt trong khoảng thời gian đã chọn."
    if "duplicate key" in msg:
        return "Mã định danh này đã tồn tại trong hệ thống."
    if "foreign key" in msg:
        return "Dữ liệu liên kết không hợp lệ hoặc đang phát sinh giao dịch."
    return "Dữ liệu nhập vào không hợp lệ, vui lòng kiểm tra lại."


# ==============================================================================
# API ĐĂNG NHẬP / ĐĂNG KÝ / ĐĂNG XUẤT
# ==============================================================================
@app.route("/api/auth/me", methods=["GET"])
def api_me():
    if "user" not in session:
        return jsonify({"authenticated": False}), 200
    # Không trả email ra thanh tài khoản theo đúng yêu cầu
    return jsonify({
        "authenticated": True,
        "user": {
            "user_id": session["user"]["user_id"],
            "name": session["user"]["name"],
            "role": session["user"]["role"],
        }
    }), 200


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not is_valid_gmail(email):
        return jsonify({"status": "error", "message": "Email phải có đuôi @gmail.com"}), 400
    if not password:
        return jsonify({"status": "error", "message": "Vui lòng nhập mật khẩu."}), 400

    pw_hash = hash_text(password)
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, name, email, phone_number, password_hash, role FROM users WHERE LOWER(email) = %s;",
                    (email,),
                )
                user = cur.fetchone()
                if not user or user["password_hash"] != pw_hash:
                    return jsonify({"status": "error", "message": "Email hoặc mật khẩu không chính xác."}), 401

                role = "system_admin" if user["role"] == "system_admin" else "customer"
                session["user"] = {
                    "user_id": user["user_id"],
                    "name": user["name"],
                    "role": role,
                }
                return jsonify({
                    "status": "success",
                    "message": "Đăng nhập thành công.",
                    "user": session["user"],
                }), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/auth/register", methods=["POST"])
def api_register():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone_number") or "").strip()
    password = data.get("password") or ""

    if not name or not email or not phone or not password:
        return jsonify({"status": "error", "message": "Vui lòng điền đầy đủ thông tin đăng ký."}), 400
    if not is_valid_gmail(email):
        return jsonify({"status": "error", "message": "Email phải có đuôi @gmail.com"}), 400

    user_id = "USR" + str(int(time.time()) % 100000).zfill(5)
    pw_hash = hash_text(password)
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, name, email, phone_number, password_hash, role) VALUES (%s, %s, %s, %s, %s, 'customer');",
                    (user_id, name, email, phone, pw_hash),
                )
            conn.commit()
        return jsonify({"status": "success", "message": "Đăng ký thành công. Vui lòng đăng nhập."}), 201
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.pop("user", None)
    return jsonify({"status": "success", "message": "Đã đăng xuất."}), 200


# ==============================================================================
# API NGHIỆP VỤ (PHÂN QUYỀN QUẢN TRỊ VÀ KHÁCH HÀNG)
# ==============================================================================
@app.route("/api/overview", methods=["GET"])
@admin_required
def api_overview():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) AS total_locations
                    FROM parking_locations;
                """)
                total_locations = cur.fetchone()["total_locations"]
                cur.execute("""
                    SELECT COUNT(*) AS total_slots,
                           COUNT(*) FILTER (WHERE status = 'Available') AS available_slots,
                           COUNT(*) FILTER (WHERE slot_type = 'Charging') AS charging_slots,
                           COUNT(*) FILTER (WHERE slot_type = 'Standard') AS standard_slots
                    FROM slots;
                """)
                parking = cur.fetchone()
                cur.execute("""
                    SELECT COUNT(*) AS total_stations,
                           COUNT(*) FILTER (WHERE status = 'Online') AS online_stations,
                           COUNT(*) FILTER (WHERE status = 'Offline') AS offline_stations,
                           COUNT(*) FILTER (WHERE status = 'Faulted') AS faulted_stations
                    FROM charging_stations;
                """)
                charging = cur.fetchone()
                cur.execute("""
                    WITH charging_totals AS (
                        SELECT ss.reservation_id,
                               SUM(ROUND(ss.energy_consumed_kwh * cr.rate_per_kwh, 0)) AS amount
                        FROM charging_sessions ss
                        JOIN charging_rates cr ON cr.rate_id=ss.rate_id
                        WHERE ss.session_status='Completed'
                        GROUP BY ss.reservation_id
                    ), fee_totals AS (
                        SELECT reservation_id, SUM(amount) AS amount
                        FROM fees
                        GROUP BY reservation_id
                    )
                    SELECT COALESCE(SUM(CASE WHEN s.slot_type='Standard'
                                             THEN r.total_amount ELSE 0 END), 0) AS parking_revenue,
                           COALESCE(SUM(CASE WHEN s.slot_type='Charging'
                                             THEN r.total_amount + COALESCE(ct.amount, 0) ELSE 0 END), 0) AS parking_charging_revenue,
                           COALESCE(SUM(COALESCE(ft.amount, 0)), 0) AS violation_revenue,
                           COALESCE(SUM(p.amount), 0) AS total_revenue
                    FROM payments p
                    JOIN reservations r ON r.reservation_id=p.reservation_id
                    JOIN slots s ON s.slot_id=r.slot_id
                    LEFT JOIN charging_totals ct ON ct.reservation_id=r.reservation_id
                    LEFT JOIN fee_totals ft ON ft.reservation_id=r.reservation_id
                    WHERE p.payment_status='Completed' AND r.payment_status='Paid';
                """)
                revenue = cur.fetchone()

        return jsonify({
            "status": "success",
            "kpis": {
                "total_locations": total_locations,
                **parking,
                **charging,
                **revenue,
            },
        }), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/users", methods=["GET", "POST"])
@admin_required
def api_users():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if request.method == "GET":
                    cur.execute("""
                        SELECT u.user_id, u.name, u.email, u.phone_number, u.role
                        FROM users u
                        ORDER BY CASE
                            WHEN u.user_id ~ '^USR[0-9]+$' THEN SUBSTRING(u.user_id FROM 4)::INTEGER
                            ELSE 2147483647
                        END, u.user_id;
                    """)
                    return jsonify({"status": "success", "data": cur.fetchall()}), 200

                data = request.get_json(force=True)
                email = (data.get("email") or "").strip().lower()
                password = data.get("password") or ""
                if not is_valid_gmail(email):
                    return jsonify({"status": "error", "message": "Email phải có đuôi @gmail.com"}), 400
                if not password.strip():
                    return jsonify({"status": "error", "message": "Vui lòng nhập mật khẩu cho người dùng mới."}), 400

                cur.execute("""
                    SELECT COALESCE(MAX(SUBSTRING(user_id FROM 4)::INTEGER), 0) + 1 AS next_id
                    FROM users
                    WHERE user_id ~ '^USR[0-9]+$';
                """)
                next_user_id = f"USR{cur.fetchone()['next_id']:03d}"

                cur.execute(
                    "INSERT INTO users (user_id, name, email, phone_number, password_hash, role) VALUES (%s, %s, %s, %s, %s, 'customer');",
                    (
                        next_user_id,
                        data.get("name"),
                        email,
                        data.get("phone_number"),
                        hash_text(password),
                    ),
                )
            conn.commit()
        return jsonify({"status": "success", "message": "Đã thêm người dùng mới."}), 201
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/users/<user_id>", methods=["DELETE"])
@admin_required
def api_delete_user(user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT role FROM users WHERE user_id = %s;", (user_id,))
                target_user = cur.fetchone()
                if target_user and target_user["role"] == "system_admin":
                    return jsonify({"status": "error", "message": "Không thể xóa tài khoản quản trị viên."}), 403
                cur.execute("DELETE FROM users WHERE user_id = %s;", (user_id,))
            conn.commit()
        return jsonify({"status": "success", "message": "Đã xóa người dùng và các phương tiện liên kết."}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/vehicles", methods=["GET", "POST"])
@customer_required
def api_vehicles():
    user = session["user"]
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if request.method == "GET":
                    if user["role"] == "customer":
                        cur.execute("""
                            SELECT v.*, u.name AS owner_name
                            FROM vehicles v JOIN users u ON v.user_id = u.user_id
                            WHERE v.user_id = %s ORDER BY v.vehicle_id;
                        """, (user["user_id"],))
                    else:
                        cur.execute("""
                            SELECT v.*, u.name AS owner_name
                            FROM vehicles v JOIN users u ON v.user_id = u.user_id
                            ORDER BY v.vehicle_id;
                        """)
                    return jsonify({"status": "success", "data": cur.fetchall()}), 200

                data = request.get_json(force=True)
                owner_id = user["user_id"] if user["role"] == "customer" else (data.get("user_id") or user["user_id"])
                cur.execute(
                    "INSERT INTO vehicles (vehicle_id, user_id, plate_number, vehicle_type) VALUES (%s, %s, %s, %s);",
                    (data.get("vehicle_id"), owner_id, (data.get("plate_number") or "").strip(), data.get("vehicle_type")),
                )
            conn.commit()
        return jsonify({"status": "success", "message": "Đăng ký phương tiện thành công."}), 201
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/vehicles/<vehicle_id>", methods=["PUT", "DELETE"])
@customer_required
def api_vehicle_detail(vehicle_id):
    user = session["user"]
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM vehicles WHERE vehicle_id = %s;", (vehicle_id,))
                vehicle = cur.fetchone()
                if not vehicle:
                    return jsonify({"status": "error", "message": "Không tìm thấy phương tiện."}), 404
                if user["role"] == "customer" and vehicle["user_id"] != user["user_id"]:
                    return jsonify({"status": "error", "message": "Bạn chỉ được sửa hoặc xóa phương tiện của mình."}), 403

                if request.method == "DELETE":
                    cur.execute("""
                        SELECT EXISTS (SELECT 1 FROM reservations WHERE vehicle_id = %s)
                            OR EXISTS (SELECT 1 FROM charging_sessions WHERE vehicle_id = %s) AS has_history;
                    """, (vehicle_id, vehicle_id))
                    if cur.fetchone()["has_history"]:
                        return jsonify({
                            "status": "error",
                            "message": "Không thể xóa phương tiện đã có lịch sử đặt chỗ hoặc sạc để bảo toàn dữ liệu."
                        }), 409
                    cur.execute("DELETE FROM vehicles WHERE vehicle_id = %s;", (vehicle_id,))
                    conn.commit()
                    return jsonify({"status": "success", "message": "Đã xóa phương tiện."}), 200

                data = request.get_json(silent=True) or {}
                new_vehicle_id = (data.get("vehicle_id") or "").strip()
                plate_number = (data.get("plate_number") or "").strip()
                vehicle_type = data.get("vehicle_type")
                if not new_vehicle_id or not plate_number or vehicle_type not in ("EV", "ICE"):
                    return jsonify({"status": "error", "message": "Vui lòng nhập đủ mã, biển số và loại phương tiện hợp lệ."}), 400
                cur.execute("""
                    UPDATE vehicles
                    SET vehicle_id = %s, plate_number = %s, vehicle_type = %s
                    WHERE vehicle_id = %s;
                """, (new_vehicle_id, plate_number, vehicle_type, vehicle_id))
            conn.commit()
        return jsonify({"status": "success", "message": "Đã cập nhật thông tin phương tiện."}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/locations", methods=["GET", "POST"])
@login_required
def api_locations():
    user = session["user"]
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if request.method == "GET":
                    if user["role"] == "system_admin":
                        cur.execute("SELECT location_id, location_name, address, status FROM parking_locations ORDER BY location_id;")
                        locations = cur.fetchall()
                        locks = locked_location_ids(cur)
                        for location in locations:
                            location["is_locked"] = location["location_id"] in locks
                            location["lock_reason"] = locks.get(location["location_id"])
                    else:
                        cur.execute("SELECT location_id, location_name, address, status FROM parking_locations WHERE status = 'Active' ORDER BY location_name;")
                        locations = cur.fetchall()
                    return jsonify({"status": "success", "data": locations}), 200
                if user["role"] != "system_admin":
                    return jsonify({"status": "error", "message": "Chỉ quản trị viên được thêm bãi đỗ."}), 403

                data = request.get_json(force=True)
                name = (data.get("location_name") or "").strip()
                address = (data.get("address") or "").strip()
                status = data.get("status", "Active")
                if not name or not address or status not in ("Active", "Maintenance", "Closed"):
                    return jsonify({"status": "error", "message": "Vui lòng nhập tên, địa chỉ và trạng thái bãi đỗ hợp lệ."}), 400
                location_id = generate_db_id(cur, "location")
                cur.execute("INSERT INTO parking_locations (location_id, location_name, address, status) VALUES (%s, %s, %s, %s);", (location_id, name, address, status))
            conn.commit()
        return jsonify({"status": "success", "message": f"Đã thêm bãi đỗ {location_id}.", "location_id": location_id}), 201
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/locations/<location_id>", methods=["GET", "PUT", "DELETE"])
@admin_required
def api_location_detail(location_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT location_id, location_name, address, status FROM parking_locations WHERE location_id = %s FOR UPDATE;", (location_id,))
                location = cur.fetchone()
                if not location:
                    return jsonify({"status": "error", "message": "Không tìm thấy bãi đỗ."}), 404
                if request.method == "GET":
                    cur.execute("SELECT COUNT(*) AS slot_count, COUNT(*) FILTER (WHERE status <> 'Available') AS unavailable_count FROM slots WHERE location_id = %s;", (location_id,))
                    counts = cur.fetchone()
                    cur.execute("SELECT EXISTS (SELECT 1 FROM reservations r JOIN slots s ON s.slot_id = r.slot_id WHERE s.location_id = %s) OR EXISTS (SELECT 1 FROM charging_sessions ss JOIN charging_stations st ON st.station_id = ss.charging_station_id JOIN slots s ON s.slot_id = st.slot_id WHERE s.location_id = %s) AS has_history;", (location_id, location_id))
                    lock_reason = location_lock_reason(cur, location_id)
                    return jsonify({"status": "success", "data": {**location, **counts, **cur.fetchone(), "is_locked": bool(lock_reason), "lock_reason": lock_reason}}), 200
                lock_reason = location_lock_reason(cur, location_id)
                if lock_reason:
                    return jsonify({"status": "error", "message": f"Không thể sửa hoặc xóa bãi đỗ. {lock_reason}"}), 409
                if request.method == "DELETE":
                    cur.execute("SELECT COUNT(*) AS slot_count, COUNT(*) FILTER (WHERE status <> 'Available') AS unavailable_count FROM slots WHERE location_id = %s;", (location_id,))
                    counts = cur.fetchone()
                    if counts["unavailable_count"]:
                        return jsonify({"status": "error", "message": "Chỉ xóa được bãi khi tất cả ô đều còn trống."}), 409
                    cur.execute("SELECT EXISTS (SELECT 1 FROM reservations r JOIN slots s ON s.slot_id = r.slot_id WHERE s.location_id = %s) OR EXISTS (SELECT 1 FROM charging_sessions ss JOIN charging_stations st ON st.station_id = ss.charging_station_id JOIN slots s ON s.slot_id = st.slot_id WHERE s.location_id = %s) AS has_history;", (location_id, location_id))
                    if cur.fetchone()["has_history"]:
                        return jsonify({"status": "error", "message": "Bãi đã có lịch sử đặt chỗ hoặc sạc nên không thể xóa để bảo toàn lịch sử."}), 409
                    cur.execute("DELETE FROM parking_locations WHERE location_id = %s;", (location_id,))
                    message = "Đã xóa bãi đỗ."
                else:
                    data = request.get_json(force=True)
                    name = (data.get("location_name") or "").strip()
                    address = (data.get("address") or "").strip()
                    status = data.get("status")
                    if not name or not address or status not in ("Active", "Maintenance", "Closed"):
                        return jsonify({"status": "error", "message": "Vui lòng nhập tên, địa chỉ và trạng thái bãi đỗ hợp lệ."}), 400
                    if status != "Active":
                        cur.execute("SELECT COUNT(*) FILTER (WHERE status <> 'Available') AS unavailable_count FROM slots WHERE location_id = %s;", (location_id,))
                        if cur.fetchone()["unavailable_count"]:
                            return jsonify({"status": "error", "message": "Chỉ được ngừng hoạt động khi tất cả ô trong bãi đều còn trống."}), 409
                        cur.execute("""
                            SELECT EXISTS (
                                SELECT 1 FROM reservations r JOIN slots s ON s.slot_id = r.slot_id
                                WHERE s.location_id = %s AND r.payment_status IN ('Pending', 'Paid') AND r.end_time > NOW()
                            ) OR EXISTS (
                                SELECT 1 FROM charging_sessions ss
                                JOIN charging_stations st ON st.station_id = ss.charging_station_id
                                JOIN slots s ON s.slot_id = st.slot_id
                                WHERE s.location_id = %s AND ss.session_status = 'Charging'
                            ) AS in_use;
                        """, (location_id, location_id))
                        if cur.fetchone()["in_use"]:
                            return jsonify({"status": "error", "message": "Bãi còn lượt đặt hoặc phiên sạc đang hoạt động nên chưa thể ngừng hoạt động."}), 409
                    cur.execute("UPDATE parking_locations SET location_name = %s, address = %s, status = %s WHERE location_id = %s;", (name, address, status, location_id))
                    message = "Đã cập nhật bãi đỗ."
            conn.commit()
        return jsonify({"status": "success", "message": message}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/stations", methods=["GET", "POST"])
@login_required
def api_stations():
    user = session["user"]
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if request.method == "GET":
                    customer_filter = "WHERE st.status = 'Online' AND s.status = 'Available' AND pl.status = 'Active'" if user["role"] == "customer" else ""
                    cur.execute(f"""
                    SELECT st.station_id, st.slot_id, st.status, s.location_id,
                           cs.charger_type, cs.kw_rate,
                           pl.location_name, s.position,
                           CONCAT(cs.charger_type, ' (', cs.kw_rate, ' kW)') AS charger_label
                    FROM charging_stations st
                    JOIN charging_slots cs ON st.slot_id = cs.slot_id
                    JOIN slots s ON cs.slot_id = s.slot_id
                    JOIN parking_locations pl ON s.location_id = pl.location_id
                    {customer_filter}
                    ORDER BY st.station_id;
                    """)
                    stations = cur.fetchall()
                    for station in stations:
                        station["lock_reason"] = slot_lock_reason(cur, station["slot_id"])
                        station["is_locked"] = bool(station["lock_reason"] or station["status"] == "Busy")
                    return jsonify({"status": "success", "data": stations}), 200

                if user["role"] != "system_admin":
                    return jsonify({"status": "error", "message": "Chỉ quản trị viên được thêm trạm sạc."}), 403
                data = request.get_json(force=True)
                slot_id = (data.get("slot_id") or "").strip()
                charger_type = (data.get("charger_type") or "").strip()
                status = data.get("status", "Online")
                location_id = (data.get("location_id") or "").strip()
                if not slot_id or not location_id or not charger_type or status not in ("Online", "Offline", "Faulted"):
                    return jsonify({"status": "error", "message": "Vui lòng nhập đủ chỗ đỗ, loại sạc và trạng thái hợp lệ."}), 400
                try:
                    kw_rate = int(data.get("kw_rate"))
                    if kw_rate < 1: raise ValueError
                except (TypeError, ValueError):
                    return jsonify({"status": "error", "message": "Công suất bộ sạc phải là số nguyên kW lớn hơn 0."}), 400
                cur.execute("""
                    SELECT s.slot_type, s.status, s.location_id, pl.status AS location_status
                    FROM slots s JOIN parking_locations pl ON pl.location_id = s.location_id
                    WHERE s.slot_id = %s FOR UPDATE OF s;
                """, (slot_id,))
                slot = cur.fetchone()
                if not slot or slot["slot_type"] != "Charging":
                    return jsonify({"status": "error", "message": "Hãy chọn chỗ đỗ có loại 'Chỗ có trụ sạc'."}), 400
                if slot["location_id"] != location_id:
                    return jsonify({"status": "error", "message": "Vị trí đã chọn không thuộc bãi/trạm sạc đã chọn."}), 400
                if slot["status"] == "Maintenance":
                    return jsonify({"status": "error", "message": "Không thể lắp trụ tại chỗ đỗ đang bảo trì."}), 409
                if slot["location_status"] != "Active":
                    return jsonify({"status": "error", "message": "Không thể lắp trụ tại bãi đang bảo trì hoặc ngừng hoạt động."}), 409
                cur.execute("SELECT 1 FROM charging_stations WHERE slot_id = %s;", (slot_id,))
                if cur.fetchone():
                    return jsonify({"status": "error", "message": "Chỗ đỗ này đã có trạm sạc."}), 409
                station_id = generate_db_id(cur, "station")
                cur.execute("""
                    INSERT INTO charging_slots (slot_id, charger_type, kw_rate)
                    VALUES (%s, %s, %s);
                """, (slot_id, charger_type, kw_rate))
                cur.execute("""
                    INSERT INTO charging_stations (station_id, slot_id, status)
                    VALUES (%s, %s, %s);
                """, (station_id, slot_id, status))
            conn.commit()
        return jsonify({"status": "success", "message": f"Đã thêm trạm sạc {station_id} tại chỗ {slot_id}.", "station_id": station_id}), 201
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/stations/<station_id>", methods=["PUT", "DELETE"])
@admin_required
def api_station_detail(station_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT slot_id, status FROM charging_stations WHERE station_id = %s FOR UPDATE;", (station_id,))
                station = cur.fetchone()
                if not station:
                    return jsonify({"status": "error", "message": "Không tìm thấy trạm sạc."}), 404
                slot_id = station["slot_id"]
                lock_reason = slot_lock_reason(cur, slot_id)
                if station["status"] == "Busy" or lock_reason:
                    reason = lock_reason or "Trụ sạc đang có phiên sạc."
                    return jsonify({"status": "error", "message": f"Không thể sửa hoặc xóa trụ sạc. {reason}"}), 409
                if request.method == "DELETE":
                    cur.execute("""
                        SELECT EXISTS (SELECT 1 FROM charging_sessions WHERE charging_station_id = %s)
                            OR EXISTS (SELECT 1 FROM reservations WHERE slot_id = %s) AS has_history;
                    """, (station_id, slot_id))
                    if cur.fetchone()["has_history"]:
                        return jsonify({"status": "error", "message": "Trạm đã có lịch sử sạc hoặc đặt chỗ nên không thể xóa. Hãy chuyển trạm sang ngoại tuyến."}), 409
                    cur.execute("DELETE FROM charging_stations WHERE station_id = %s;", (station_id,))
                    cur.execute("DELETE FROM charging_slots WHERE slot_id = %s;", (slot_id,))
                    cur.execute("UPDATE slots SET slot_type = 'Standard' WHERE slot_id = %s;", (slot_id,))
                    message = "Đã xóa trạm sạc; chỗ đỗ được chuyển thành chỗ tiêu chuẩn."
                else:
                    data = request.get_json(force=True)
                    if station["status"] == "Busy":
                        return jsonify({"status": "error", "message": "Trạm đang có phiên sạc; hãy kết thúc phiên trước khi sửa cấu hình."}), 409
                    charger_type = (data.get("charger_type") or "").strip()
                    status = data.get("status")
                    if not charger_type or status not in ("Online", "Offline", "Faulted"):
                        return jsonify({"status": "error", "message": "Thông tin loại sạc hoặc trạng thái không hợp lệ."}), 400
                    try:
                        kw_rate = int(data.get("kw_rate"))
                        if kw_rate < 1: raise ValueError
                    except (TypeError, ValueError):
                        return jsonify({"status": "error", "message": "Công suất bộ sạc phải là số nguyên kW lớn hơn 0."}), 400
                    if status == "Online":
                        cur.execute("SELECT status FROM slots WHERE slot_id = %s;", (slot_id,))
                        if cur.fetchone()["status"] == "Maintenance":
                            return jsonify({"status": "error", "message": "Không thể bật trạm khi chỗ đỗ đang bảo trì."}), 409
                    cur.execute("UPDATE charging_stations SET status = %s WHERE station_id = %s;", (status, station_id))
                    cur.execute("UPDATE charging_slots SET charger_type = %s, kw_rate = %s WHERE slot_id = %s;", (charger_type, kw_rate, slot_id))
                    message = "Đã cập nhật trạm sạc và thông số cổng."
            conn.commit()
        return jsonify({"status": "success", "message": message}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/slots", methods=["GET"])
@login_required
def api_slots():
    location_id = request.args.get("location_id")
    slot_type = request.args.get("slot_type")
    vehicle_type = request.args.get("vehicle_type")
    only_available = request.args.get("only_available") == "true"
    admin_only_available = session["user"].get("role") == "system_admin" and request.args.get("admin_only_available") == "true"
    start_value = (request.args.get("start_time") or "").replace("T", " ")
    end_value = (request.args.get("end_time") or "").replace("T", " ")
    has_window = bool(start_value and end_value)
    if bool(start_value) != bool(end_value):
        return jsonify({"status": "error", "message": "Vui lòng chọn đủ thời gian bắt đầu và kết thúc."}), 400
    if has_window:
        try:
            start_dt = parse_local_datetime(start_value)
            end_dt = parse_local_datetime(end_value)
        except ValueError:
            return jsonify({"status": "error", "message": "Khoảng thời gian tìm kiếm không hợp lệ."}), 400
        if start_dt <= datetime.now(APP_TIMEZONE) or end_dt <= start_dt:
            return jsonify({"status": "error", "message": "Thời gian bắt đầu phải ở tương lai và thời gian kết thúc phải sau thời gian bắt đầu."}), 400
    else:
        start_dt = end_dt = None

    sql = """
        SELECT s.slot_id, s.location_id, pl.location_name, pl.address,
               s.position, s.slot_type, s.status,
               cs.charger_type, cs.kw_rate, st.station_id
        FROM slots s
        JOIN parking_locations pl ON s.location_id = pl.location_id
        LEFT JOIN charging_slots cs ON s.slot_id = cs.slot_id
        LEFT JOIN charging_stations st ON cs.slot_id = st.slot_id
        WHERE (%s IS NULL OR %s = '' OR s.location_id = %s)
          AND pl.status = 'Active'
          AND (%s = FALSE OR s.status = 'Available')
          AND (%s IS NULL OR %s = '' OR s.slot_type = %s)
          AND (
              %s = FALSE OR (
                  s.status NOT IN ('Occupied', 'Maintenance')
                  AND (s.slot_type <> 'Charging' OR st.status = 'Online')
                  AND (%s = TRUE OR s.status = 'Available')
                  AND (
                      %s = FALSE OR (
                          NOT EXISTS (
                              SELECT 1 FROM reservations r
                              WHERE r.slot_id = s.slot_id
                                AND r.payment_status IN ('Pending', 'Paid')
                                AND r.start_time < %s AND r.end_time > %s
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM charging_sessions ss
                              WHERE ss.charging_station_id = st.station_id
                                AND ss.start_date_time < %s
                                AND COALESCE(
                                    ss.vehicle_departed_at,
                                    CASE WHEN ss.session_status = 'Completed'
                                         THEN GREATEST(
                                             ss.end_date_time,
                                             COALESCE((SELECT r.end_time FROM reservations r WHERE r.reservation_id=ss.reservation_id), ss.end_date_time)
                                         ) + INTERVAL '15 minutes'
                                         ELSE 'infinity'::timestamptz END
                                ) > %s
                                AND (ss.session_status = 'Charging' OR ss.vehicle_departed_at IS NULL)
                          )
                      )
                  )
              )
          )
        ORDER BY s.slot_id;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    location_id, location_id, location_id, admin_only_available,
                    slot_type, slot_type, slot_type,
                    only_available, has_window, has_window,
                    end_dt, start_dt, end_dt, start_dt
                ))
                rows = cur.fetchall()
                for row in rows:
                    row["lock_reason"] = slot_lock_reason(cur, row["slot_id"])
                    row["is_locked"] = bool(row["lock_reason"])
                if has_window:
                    rates_by_location = {}
                    priced_rows = []
                    for row in rows:
                        location_key = row["location_id"]
                        service_type = "both" if row["slot_type"] == "Charging" else "parking"
                        rate_key = (location_key, service_type)
                        if rate_key not in rates_by_location:
                            rates_by_location[rate_key] = latest_rates(cur, "parking_rates", location_key, service_type)
                        try:
                            parking_estimate = calculate_dynamic_parking_fee(start_dt, end_dt, rates_by_location[rate_key])
                        except ValueError:
                            # Chỉ bỏ vị trí khi có ít nhất một đoạn thời gian chưa được cấu hình giá.
                            continue
                        violation_estimate = 0
                        if vehicle_type == "ICE" and row["slot_type"] == "Charging":
                            cur.execute("SELECT amount FROM violation_rates WHERE location_id = %s ORDER BY created_at DESC, rate_id DESC LIMIT 1;", (location_key,))
                            violation = cur.fetchone()
                            violation_estimate = float(violation["amount"]) if violation else 0
                        row["estimated_parking_fee"] = parking_estimate
                        row["estimated_total"] = parking_estimate
                        priced_rows.append(row)
                    rows = priced_rows
        return jsonify({"status": "success", "data": rows}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 409


@app.route("/api/slots", methods=["POST"])
@admin_required
def api_create_slot():
    data = request.get_json(force=True)
    location_id = (data.get("location_id") or "").strip()
    slot_type = data.get("slot_type")
    try:
        position = int(data.get("position", 0))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Vị trí ô phải là số nguyên dương."}), 400
    if not location_id or slot_type not in ("Standard", "Charging") or position < 1:
        return jsonify({"status": "error", "message": "Vui lòng chọn bãi đỗ, loại chỗ và vị trí hợp lệ."}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM parking_locations WHERE location_id = %s;", (location_id,))
                location = cur.fetchone()
                if not location:
                    return jsonify({"status": "error", "message": "Không tìm thấy bãi đỗ."}), 404
                if location["status"] != "Active":
                    return jsonify({"status": "error", "message": "Chỉ được thêm chỗ vào bãi đang hoạt động."}), 409
                cur.execute("SELECT 1 FROM slots WHERE location_id = %s AND position = %s;", (location_id, position))
                if cur.fetchone():
                    return jsonify({"status": "error", "message": "Vị trí này đã được sử dụng trong bãi đỗ đã chọn."}), 409
                slot_id = generate_db_id(cur, "slot")
                cur.execute("INSERT INTO slots (slot_id, location_id, status, slot_type, position) VALUES (%s, %s, 'Available', %s, %s);", (slot_id, location_id, slot_type, position))
            conn.commit()
        return jsonify({"status": "success", "message": f"Đã thêm chỗ đỗ {slot_id}.", "slot_id": slot_id}), 201
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/slots/<slot_id>", methods=["PUT", "DELETE"])
@admin_required
def api_slot_detail(slot_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM slots WHERE slot_id = %s FOR UPDATE;", (slot_id,))
                slot = cur.fetchone()
                if not slot:
                    return jsonify({"status": "error", "message": "Không tìm thấy chỗ đỗ."}), 404
                lock_reason = slot_lock_reason(cur, slot_id)
                if lock_reason:
                    return jsonify({"status": "error", "message": f"Không thể sửa hoặc xóa vị trí. {lock_reason}"}), 409
                if request.method == "DELETE":
                    cur.execute("""
                        SELECT EXISTS (SELECT 1 FROM reservations WHERE slot_id = %s)
                            OR EXISTS (
                                SELECT 1 FROM charging_sessions ss
                                JOIN charging_stations st ON st.station_id = ss.charging_station_id
                                WHERE st.slot_id = %s
                            ) AS has_history,
                            EXISTS (SELECT 1 FROM charging_stations WHERE slot_id = %s) AS has_station;
                    """, (slot_id, slot_id, slot_id))
                    checks = cur.fetchone()
                    if checks["has_history"]:
                        return jsonify({"status": "error", "message": "Chỗ đỗ đã có lịch sử đặt chỗ hoặc sạc nên không thể xóa."}), 409
                    if checks["has_station"]:
                        return jsonify({"status": "error", "message": "Hãy xóa trạm sạc gắn với chỗ này trước."}), 409
                    cur.execute("DELETE FROM slots WHERE slot_id = %s;", (slot_id,))
                    message = "Đã xóa chỗ đỗ."
                else:
                    data = request.get_json(force=True)
                    location_id = (data.get("location_id") or "").strip()
                    slot_type = data.get("slot_type")
                    try:
                        position = int(data.get("position", 0))
                    except (TypeError, ValueError):
                        return jsonify({"status": "error", "message": "Vị trí ô phải là số nguyên dương."}), 400
                    if not location_id or slot_type not in ("Standard", "Charging") or position < 1:
                        return jsonify({"status": "error", "message": "Thông tin bãi đỗ, loại chỗ hoặc vị trí không hợp lệ."}), 400
                    cur.execute("SELECT status FROM parking_locations WHERE location_id = %s;", (location_id,))
                    location = cur.fetchone()
                    if not location or location["status"] != "Active":
                        return jsonify({"status": "error", "message": "Chỗ đỗ chỉ được gắn với bãi đang hoạt động."}), 409
                    if slot_type != slot["slot_type"]:
                        cur.execute("""
                            SELECT EXISTS (SELECT 1 FROM reservations WHERE slot_id = %s)
                                OR EXISTS (
                                    SELECT 1 FROM charging_sessions ss JOIN charging_stations st ON st.station_id = ss.charging_station_id
                                    WHERE st.slot_id = %s
                                ) AS has_history;
                        """, (slot_id, slot_id))
                        if cur.fetchone()["has_history"]:
                            return jsonify({"status": "error", "message": "Không thể đổi loại chỗ đỗ đã có lịch sử đặt chỗ hoặc sạc."}), 409
                    if slot_type == "Standard" and slot["slot_type"] == "Charging":
                        cur.execute("SELECT EXISTS (SELECT 1 FROM charging_slots WHERE slot_id = %s) AS has_charger;", (slot_id,))
                        if cur.fetchone()["has_charger"]:
                            return jsonify({"status": "error", "message": "Hãy xóa cấu hình trạm sạc trước khi chuyển chỗ này thành chỗ tiêu chuẩn."}), 409
                    cur.execute("SELECT 1 FROM slots WHERE location_id = %s AND position = %s AND slot_id <> %s;", (location_id, position, slot_id))
                    if cur.fetchone():
                        return jsonify({"status": "error", "message": "Vị trí này đã được sử dụng trong bãi đỗ đã chọn."}), 409
                    cur.execute("UPDATE slots SET location_id = %s, slot_type = %s, position = %s WHERE slot_id = %s;", (location_id, slot_type, position, slot_id))
                    message = "Đã cập nhật chỗ đỗ."
            conn.commit()
        return jsonify({"status": "success", "message": message}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/slots/<slot_id>/status", methods=["PUT"])
@admin_required
def api_update_slot_status(slot_id):
    data = request.get_json(force=True)
    new_status = data.get("status")
    if new_status not in ("Available", "Maintenance"):
        return jsonify({"status": "error", "message": "Admin chỉ có thể chuyển chỗ sang Trống hoặc Bảo trì; trạng thái Có xe do phiên sạc cập nhật."}), 400
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM slots WHERE slot_id = %s FOR UPDATE;", (slot_id,))
                slot = cur.fetchone()
                if not slot:
                    return jsonify({"status": "error", "message": "Không tìm thấy chỗ đỗ."}), 404
                if slot["status"] == "Occupied":
                    return jsonify({"status": "error", "message": "Vị trí đang có xe; không thể đổi trạng thái."}), 409
                if new_status == "Maintenance" and slot["status"] in ("Occupied", "Reserved"):
                    return jsonify({"status": "error", "message": "Không thể đưa chỗ đang có xe hoặc được giữ vào bảo trì."}), 409
                if new_status == "Maintenance":
                    cur.execute("""
                        SELECT EXISTS (SELECT 1 FROM reservations WHERE slot_id = %s AND payment_status IN ('Pending', 'Paid') AND end_time > NOW())
                            OR EXISTS (
                                SELECT 1 FROM charging_sessions ss
                                JOIN charging_stations st ON st.station_id = ss.charging_station_id
                                WHERE st.slot_id = %s AND ss.session_status = 'Charging'
                            ) AS in_use;
                    """, (slot_id, slot_id))
                    if cur.fetchone()["in_use"]:
                        return jsonify({"status": "error", "message": "Chỗ này còn lượt đặt hoặc phiên sạc đang hoạt động."}), 409
                cur.execute("UPDATE slots SET status = %s WHERE slot_id = %s;", (new_status, slot_id))
            conn.commit()
        return jsonify({"status": "success", "message": f"Đã cập nhật trạng thái chỗ đỗ {slot_id}."}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/reservations", methods=["GET", "POST"])
@customer_required
def api_reservations():
    user = session["user"]
    try:
        if request.method == "GET":
            auto_finish_expired_charging_sessions()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if request.method == "GET":
                    if user["role"] == "customer":
                        cur.execute("""
                            SELECT r.*, v.plate_number, v.vehicle_type, u.name AS customer_name, s.slot_type,
                                   s.position, pl.location_name,
                                   EXISTS (SELECT 1 FROM charging_sessions ss WHERE ss.reservation_id=r.reservation_id) AS has_charging_session
                            FROM reservations r
                            JOIN vehicles v ON r.vehicle_id = v.vehicle_id
                            JOIN users u ON v.user_id = u.user_id
                            JOIN slots s ON r.slot_id = s.slot_id
                            JOIN parking_locations pl ON pl.location_id=s.location_id
                            WHERE u.user_id = %s AND r.payment_status = 'Pending'
                            ORDER BY r.start_time DESC;
                        """, (user["user_id"],))
                    else:
                        cur.execute("""
                            SELECT r.*, v.plate_number, v.vehicle_type, u.name AS customer_name, s.slot_type,
                                   s.position, pl.location_name,
                                   EXISTS (SELECT 1 FROM charging_sessions ss WHERE ss.reservation_id=r.reservation_id) AS has_charging_session
                            FROM reservations r
                            JOIN vehicles v ON r.vehicle_id = v.vehicle_id
                            JOIN users u ON v.user_id = u.user_id
                            JOIN slots s ON r.slot_id = s.slot_id
                            JOIN parking_locations pl ON pl.location_id=s.location_id
                            WHERE r.payment_status = 'Pending'
                            ORDER BY r.start_time DESC;
                        """)
                    return jsonify({"status": "success", "data": cur.fetchall()}), 200

                data = request.get_json(force=True)
                res_id = generate_reservation_id(cur)
                vehicle_id = (data.get("vehicle_id") or "").strip()
                slot_id = (data.get("slot_id") or "").strip()
                start_str = (data.get("start_time") or "").replace("T", " ")
                end_str = (data.get("end_time") or "").replace("T", " ")

                try:
                    start_dt = parse_local_datetime(start_str)
                    end_dt = parse_local_datetime(end_str)
                except ValueError:
                    return jsonify({"status": "error", "message": "Thời gian nhập vào không hợp lệ."}), 400

                if start_dt <= datetime.now(APP_TIMEZONE):
                    return jsonify({"status": "error", "message": "Thời gian bắt đầu đặt chỗ phải ở tương lai."}), 400
                if end_dt <= start_dt:
                    return jsonify({"status": "error", "message": "Thời gian kết thúc phải sau thời gian bắt đầu."}), 400

                cur.execute("SELECT user_id, vehicle_type FROM vehicles WHERE vehicle_id = %s;", (vehicle_id,))
                veh_row = cur.fetchone()
                if not veh_row:
                    return jsonify({"status": "error", "message": "Không tìm thấy phương tiện."}), 404
                if user["role"] == "customer" and veh_row["user_id"] != user["user_id"]:
                    return jsonify({"status": "error", "message": "Bạn chỉ được đặt chỗ cho xe của mình."}), 403

                # Khóa dòng chỗ đỗ để hai yêu cầu đồng thời không cùng xác nhận một khung giờ.
                cur.execute("""
                    SELECT s.slot_type, s.status, s.location_id, pl.status AS location_status
                    FROM slots s JOIN parking_locations pl ON pl.location_id = s.location_id
                    WHERE s.slot_id = %s FOR UPDATE OF s;
                """, (slot_id,))
                slot_row = cur.fetchone()
                if not slot_row:
                    return jsonify({"status": "error", "message": "Không tìm thấy chỗ đỗ đã chọn."}), 404
                if slot_row["status"] in ("Occupied", "Maintenance"):
                    return jsonify({"status": "error", "message": "Chỗ đỗ đang được sử dụng hoặc bảo trì."}), 409
                if slot_row["location_status"] != "Active":
                    return jsonify({"status": "error", "message": "Bãi đỗ đang bảo trì hoặc ngừng hoạt động."}), 409
                if slot_row["slot_type"] == "Charging":
                    cur.execute("SELECT status FROM charging_stations WHERE slot_id = %s;", (slot_id,))
                    station_status = cur.fetchone()
                    if not station_status or station_status["status"] != "Online":
                        return jsonify({"status": "error", "message": "Trụ sạc tại chỗ này hiện không hoạt động."}), 409
                cur.execute("""
                    SELECT 1 FROM reservations
                    WHERE slot_id = %s AND payment_status IN ('Pending', 'Paid')
                      AND start_time < %s AND end_time > %s
                    LIMIT 1;
                """, (slot_id, end_dt, start_dt))
                if cur.fetchone():
                    return jsonify({"status": "error", "message": "Chỗ đỗ vừa được người khác đặt trong khoảng thời gian này. Hãy tìm lại chỗ."}), 409

                service_type = "both" if slot_row["slot_type"] == "Charging" else "parking"
                rates_list = latest_rates(cur, "parking_rates", slot_row["location_id"], service_type)
                rate_row = rate_for_time(rates_list, start_dt.astimezone(APP_TIMEZONE).time().replace(tzinfo=None), "price_per_hour")
                if not rate_row:
                    return jsonify({"status": "error", "message": "Chưa cấu hình đơn giá đỗ xe cho thời điểm bắt đầu."}), 409
                rate_id = rate_row["rate_id"]
                dynamic_parking_fee = calculate_dynamic_parking_fee(start_dt, end_dt, rates_list)

                # Phí vi phạm chỉ phát sinh nếu xe chưa rời chỗ sau thời gian
                # kết thúc đặt chỗ cộng thêm 15 phút; đặt xe xăng vào chỗ sạc
                # không tự tạo phí ngay lúc đặt.
                total_initial = dynamic_parking_fee

                cur.execute("""
                    INSERT INTO reservations (reservation_id, vehicle_id, slot_id, rate_id, start_time, end_time, total_amount, payment_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'Pending');
                """, (res_id, vehicle_id, slot_id, rate_id, start_str, end_str, total_initial))

            conn.commit()

        msg = f"Đặt chỗ thành công. Chi phí đỗ xe tạm tính: {int(dynamic_parking_fee):,} VNĐ. Sau khi hết thời gian đặt chỗ, vui lòng di chuyển xe trong vòng 15 phút để tránh phát sinh phí vi phạm."
        return jsonify({"status": "success", "message": msg, "reservation_id": res_id}), 201
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 409
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/reservations/<reservation_id>/depart", methods=["PUT"])
@customer_required
def api_depart_reservation(reservation_id):
    user = session["user"]
    auto_finish_expired_charging_sessions()
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT r.reservation_id, r.start_time, r.end_time, r.payment_status, r.vehicle_departed_at,
                           v.user_id, s.slot_id, s.location_id
                    FROM reservations r
                    JOIN vehicles v ON v.vehicle_id=r.vehicle_id
                    JOIN slots s ON s.slot_id=r.slot_id
                    WHERE r.reservation_id=%s FOR UPDATE OF r, s;
                """, (reservation_id,))
                reservation = cur.fetchone()
                if not reservation:
                    return jsonify({"status": "error", "message": "Không tìm thấy mã đặt chỗ."}), 404
                if reservation["user_id"] != user["user_id"]:
                    return jsonify({"status": "error", "message": "Bạn chỉ xác nhận rời chỗ cho đặt chỗ của mình."}), 403
                if reservation["payment_status"] != "Pending":
                    return jsonify({"status": "error", "message": "Đặt chỗ này không còn chờ xử lý."}), 409
                if reservation["vehicle_departed_at"] is not None:
                    return jsonify({"status": "error", "message": "Mã đặt chỗ này đã được xác nhận rời chỗ."}), 409

                now = datetime.now(APP_TIMEZONE)
                if now < reservation["start_time"]:
                    return jsonify({"status": "error", "message": "Chỉ có thể xác nhận rời chỗ khi đã đến giờ bắt đầu đặt chỗ."}), 409

                cur.execute("SELECT 1 FROM charging_sessions WHERE reservation_id=%s AND session_status='Charging' LIMIT 1;", (reservation_id,))
                if cur.fetchone():
                    return jsonify({"status": "error", "message": "Phiên sạc vẫn đang hoạt động. Hãy kết thúc sạc trước khi xác nhận rời chỗ."}), 409

                cur.execute("UPDATE reservations SET vehicle_departed_at=%s WHERE reservation_id=%s RETURNING vehicle_departed_at;", (now, reservation_id))
                departed_at = cur.fetchone()["vehicle_departed_at"]
                grace_end = reservation["end_time"] + timedelta(minutes=15)
                late_seconds = max(0, (departed_at - grace_end).total_seconds())
                late_minutes = math.ceil(late_seconds / 60) if late_seconds else 0
                violation_amount = 0.0
                if late_minutes > 0:
                    cur.execute("SELECT amount FROM violation_rates WHERE location_id=%s ORDER BY created_at DESC, rate_id DESC LIMIT 1;", (reservation["location_id"],))
                    rate = cur.fetchone()
                    if not rate:
                        return jsonify({"status": "error", "message": "Bãi đỗ chưa được cấu hình phí vi phạm; chưa thể hoàn tất xác nhận rời chỗ."}), 409
                    violation_amount = float(rate["amount"])
                    cur.execute("""
                        INSERT INTO fees (fee_id, reservation_id, fee_type, amount, excess_duration)
                        SELECT %s, %s, 'Idle', %s, %s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM fees WHERE reservation_id=%s AND fee_type='Idle'
                        );
                    """, ("FEE" + uuid.uuid4().hex[:12].upper(), reservation_id, violation_amount, late_minutes, reservation_id))

                cur.execute("UPDATE charging_sessions SET vehicle_departed_at=%s WHERE reservation_id=%s AND session_status='Completed' AND vehicle_departed_at IS NULL;", (departed_at, reservation_id))
                cur.execute("UPDATE slots SET status='Available' WHERE slot_id=%s AND status='Occupied';", (reservation["slot_id"],))
            conn.commit()
        if violation_amount:
            msg = f"Bạn đã xác nhận rời chỗ sau thời gian miễn phí 15 phút. Phí vi phạm {int(violation_amount):,} VNĐ đã được cộng vào hóa đơn thanh toán."
        else:
            msg = "Đã ghi nhận xe rời chỗ đúng hạn. Chưa phát sinh phí vi phạm."
        return jsonify({"status": "success", "message": msg}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/sessions", methods=["GET", "POST"])
@customer_required
def api_sessions():
    user = session["user"]
    try:
        auto_finish_expired_charging_sessions()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if request.method == "GET":
                    if user["role"] == "customer":
                        cur.execute("""
                            SELECT ss.*, v.plate_number, cr.rate_type, cr.rate_per_kwh,
                                   v.vehicle_id, r.payment_status,
                                   ROUND(ss.energy_consumed_kwh * cr.rate_per_kwh, 0) AS charging_cost
                            FROM charging_sessions ss
                            JOIN vehicles v ON ss.vehicle_id = v.vehicle_id
                            JOIN charging_rates cr ON ss.rate_id = cr.rate_id
                            JOIN reservations r ON ss.reservation_id = r.reservation_id
                            WHERE v.user_id = %s AND (
                                ss.session_status = 'Charging'
                                OR (ss.session_status = 'Completed' AND r.payment_status = 'Pending')
                            )
                            ORDER BY ss.start_date_time DESC;
                        """, (user["user_id"],))
                    else:
                        cur.execute("""
                            SELECT ss.*, v.plate_number, cr.rate_type, cr.rate_per_kwh,
                                   v.vehicle_id, r.payment_status,
                                   ROUND(ss.energy_consumed_kwh * cr.rate_per_kwh, 0) AS charging_cost
                            FROM charging_sessions ss
                            JOIN vehicles v ON ss.vehicle_id = v.vehicle_id
                            JOIN charging_rates cr ON ss.rate_id = cr.rate_id
                            JOIN reservations r ON ss.reservation_id = r.reservation_id
                            WHERE ss.session_status = 'Charging'
                               OR (ss.session_status = 'Completed' AND r.payment_status = 'Pending')
                            ORDER BY ss.start_date_time DESC;
                        """)
                    return jsonify({"status": "success", "data": cur.fetchall()}), 200

                data = request.get_json(force=True)
                vehicle_id = (data.get("vehicle_id") or "").strip()
                reservation_id = (data.get("reservation_id") or "").strip()
                if not reservation_id:
                    return jsonify({"status": "error", "message": "Hãy chọn mã đặt chỗ để bắt đầu phiên sạc."}), 400

                cur.execute("SELECT user_id, vehicle_type FROM vehicles WHERE vehicle_id = %s;", (vehicle_id,))
                vehicle = cur.fetchone()
                if not vehicle:
                    return jsonify({"status": "error", "message": "Không tìm thấy phương tiện."}), 404
                if user["role"] == "customer" and vehicle["user_id"] != user["user_id"]:
                    return jsonify({"status": "error", "message": "Bạn chỉ được bắt đầu sạc cho phương tiện của mình."}), 403
                if vehicle["vehicle_type"] != "EV":
                    return jsonify({"status": "error", "message": "Chỉ xe điện mới có thể bắt đầu phiên sạc."}), 400

                cur.execute("""
                    SELECT r.vehicle_id, r.slot_id, r.payment_status, r.start_time, r.end_time, r.vehicle_departed_at,
                           s.slot_type, s.status AS slot_status
                    FROM reservations r JOIN slots s ON s.slot_id=r.slot_id
                    WHERE r.reservation_id=%s FOR UPDATE OF r, s;
                """, (reservation_id,))
                reservation = cur.fetchone()
                if not reservation:
                    return jsonify({"status": "error", "message": "Không tìm thấy mã đặt chỗ."}), 404
                if reservation["vehicle_id"] != vehicle_id:
                    return jsonify({"status": "error", "message": "Mã đặt chỗ không thuộc về xe đã chọn."}), 400
                if reservation["slot_type"] != "Charging":
                    return jsonify({"status": "error", "message": "Mã đặt chỗ này không dành cho chỗ có trụ sạc."}), 400
                if reservation["payment_status"] != "Pending":
                    return jsonify({"status": "error", "message": "Chỉ được bắt đầu sạc với mã đặt chỗ đang chờ thanh toán."}), 409
                if reservation["vehicle_departed_at"] is not None:
                    return jsonify({"status": "error", "message": "Mã đặt chỗ này đã được xác nhận rời chỗ nên không thể bắt đầu sạc."}), 409
                now = datetime.now(APP_TIMEZONE)
                if now < reservation["start_time"]:
                    return jsonify({"status": "error", "message": "Chưa đến giờ bắt đầu đặt chỗ; bạn hãy quay lại đúng giờ để sạc."}), 409
                if now >= reservation["end_time"]:
                    return jsonify({"status": "error", "message": "Thời gian của mã đặt chỗ đã kết thúc."}), 409
                if reservation["slot_status"] in ("Occupied", "Maintenance"):
                    return jsonify({"status": "error", "message": "Chỗ sạc đang được sử dụng hoặc bảo trì."}), 409
                cur.execute("SELECT 1 FROM charging_sessions WHERE reservation_id=%s LIMIT 1;", (reservation_id,))
                if cur.fetchone():
                    return jsonify({"status": "error", "message": "Mã đặt chỗ này đã được dùng cho một phiên sạc; không thể tính phí sạc lần nữa."}), 409

                cur.execute("""
                    SELECT st.station_id, st.status, st.slot_id, cs.kw_rate
                    FROM charging_stations st JOIN charging_slots cs ON st.slot_id=cs.slot_id
                    WHERE st.slot_id=%s FOR UPDATE OF st;
                """, (reservation["slot_id"],))
                station = cur.fetchone()
                if not station:
                    return jsonify({"status": "error", "message": "Vị trí trong mã đặt chỗ chưa có trụ sạc."}), 404
                if station["status"] != "Online":
                    return jsonify({"status": "error", "message": "Trụ sạc tại vị trí đặt chỗ hiện không sẵn sàng."}), 409

                now = datetime.now(APP_TIMEZONE)
                station_id = station["station_id"]
                cur.execute("SELECT s.location_id FROM charging_stations st JOIN slots s ON s.slot_id=st.slot_id WHERE st.station_id=%s;", (station_id,))
                station_location = cur.fetchone()
                rate = rate_for_time(latest_rates(cur, "charging_rates", station_location["location_id"]), now.astimezone(APP_TIMEZONE).time().replace(tzinfo=None))
                if not rate:
                    return jsonify({"status": "error", "message": "Chưa cấu hình đơn giá sạc cho thời điểm hiện tại."}), 409

                session_id = "SES" + uuid.uuid4().hex[:12].upper()
                cur.execute("""
                    INSERT INTO charging_sessions (
                        session_id, vehicle_id, charging_station_id, rate_id,
                        start_date_time, end_date_time, energy_consumed_kwh,
                        session_status, reservation_id
                    ) VALUES (%s, %s, %s, %s, %s, NULL, NULL, 'Charging', %s);
                """, (session_id, vehicle_id, station_id, rate["rate_id"], now, reservation_id))
                cur.execute("UPDATE charging_stations SET status = 'Busy' WHERE station_id = %s;", (station_id,))
                cur.execute("UPDATE slots SET status = 'Occupied' WHERE slot_id = %s;", (station["slot_id"],))
            conn.commit()
        return jsonify({
            "status": "success",
            "message": "Đã bắt đầu phiên sạc. Khi hoàn tất, hãy bấm Kết thúc sạc để ghi nhận điện năng ước tính.",
            "session_id": session_id
        }), 201
    except psycopg2.Error as e:
        if "idx_one_active_session_per_" in (e.pgerror or ""):
            return jsonify({"status": "error", "message": "Xe hoặc trụ sạc đang có một phiên sạc khác."}), 409
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/sessions/<session_id>/finish", methods=["PUT"])
@customer_required
def api_finish_session(session_id):
    user = session["user"]
    auto_finish_expired_charging_sessions()
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ss.*, v.user_id, cs.kw_rate
                    FROM charging_sessions ss
                    JOIN vehicles v ON ss.vehicle_id = v.vehicle_id
                    JOIN charging_stations st ON ss.charging_station_id = st.station_id
                    JOIN charging_slots cs ON st.slot_id = cs.slot_id
                    WHERE ss.session_id = %s FOR UPDATE OF ss;
                """, (session_id,))
                charging = cur.fetchone()
                if not charging:
                    return jsonify({"status": "error", "message": "Không tìm thấy phiên sạc."}), 404
                if user["role"] == "customer" and charging["user_id"] != user["user_id"]:
                    return jsonify({"status": "error", "message": "Bạn chỉ được kết thúc phiên sạc của mình."}), 403
                if charging["session_status"] != "Charging":
                    return jsonify({"status": "error", "message": "Phiên sạc này đã được kết thúc."}), 409

                end_dt = datetime.now(APP_TIMEZONE)
                duration_hours = max(0.0, (end_dt - charging["start_date_time"]).total_seconds() / 3600.0)
                energy_kwh = round(max(0.5, duration_hours * float(charging["kw_rate"]) * 0.85), 2)
                cur.execute("SELECT rate_per_kwh FROM charging_rates WHERE rate_id = %s;", (charging["rate_id"],))
                rate = cur.fetchone()
                charging_cost = round(energy_kwh * float(rate["rate_per_kwh"]), 0)
                cur.execute("""
                    UPDATE charging_sessions
                    SET end_date_time = %s, energy_consumed_kwh = %s, session_status = 'Completed'
                    WHERE session_id = %s;
                """, (end_dt, energy_kwh, session_id))
                cur.execute("UPDATE charging_stations SET status = 'Online' WHERE station_id = %s;", (charging["charging_station_id"],))
            conn.commit()
        return jsonify({
            "status": "success",
            "message": f"Đã kết thúc phiên sạc. Phí sạc {int(charging_cost):,} VNĐ đã được ghi nhận để thanh toán cùng mã đặt chỗ. Hãy di chuyển xe trong vòng 15 phút kể từ khi hết thời gian đặt chỗ để tránh phát sinh phí vi phạm.",
            "energy_kwh": energy_kwh,
            "charging_cost": charging_cost
        }), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/billing", methods=["GET"])
@customer_required
def api_billing():
    user = session["user"]
    try:
        auto_finish_expired_charging_sessions()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT r.reservation_id, r.vehicle_id, r.slot_id, r.start_time,
                           r.end_time, r.total_amount AS parking_cost, r.vehicle_departed_at, s.slot_type,
                           s.position, pl.location_name, v.plate_number, v.vehicle_type
                    FROM reservations r
                    JOIN vehicles v ON v.vehicle_id=r.vehicle_id
                    JOIN slots s ON s.slot_id=r.slot_id
                    JOIN parking_locations pl ON pl.location_id=s.location_id
                    WHERE v.user_id=%s AND r.payment_status='Pending'
                      AND r.start_time <= %s AND r.vehicle_departed_at IS NOT NULL
                    ORDER BY r.start_time;
                """, (user["user_id"], datetime.now(APP_TIMEZONE)))
                payable = []
                for reservation in cur.fetchall():
                    cur.execute("""
                        SELECT COALESCE(SUM(ROUND(ss.energy_consumed_kwh * cr.rate_per_kwh, 0)), 0) AS amount,
                               COUNT(*) AS completed_sessions
                        FROM charging_sessions ss
                        JOIN charging_rates cr ON cr.rate_id=ss.rate_id
                        WHERE ss.reservation_id=%s AND ss.session_status='Completed';
                    """, (reservation["reservation_id"],))
                    charging = cur.fetchone()
                    if (reservation["slot_type"] == "Charging"
                            and reservation["vehicle_type"] == "EV"
                            and charging["completed_sessions"] == 0):
                        continue
                    cur.execute("SELECT COALESCE(SUM(amount), 0) AS amount FROM fees WHERE reservation_id=%s;", (reservation["reservation_id"],))
                    violation = cur.fetchone()
                    reservation["charging_cost"] = float(charging["amount"] or 0)
                    reservation["violation_cost"] = float(violation["amount"] or 0)
                    reservation["total_cost"] = float(reservation["parking_cost"] or 0) + reservation["charging_cost"] + reservation["violation_cost"]
                    payable.append(reservation)
        return jsonify({"status": "success", "payable": payable}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/payments/pay", methods=["POST"])
@customer_required
def api_pay():
    user = session["user"]
    data = request.get_json(force=True)
    res_id = (data.get("reservation_id") or "").strip()
    raw_method = data.get("payment_method", "Ví điện tử")
    encrypted_method = "ENC_" + hash_text(raw_method)[:16].upper()
    pay_id = "PAY" + uuid.uuid4().hex[:12].upper()

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT r.reservation_id, r.total_amount AS parking_cost, r.slot_id,
                           r.start_time, r.end_time, r.vehicle_departed_at, r.payment_status,
                           v.user_id, s.slot_type, v.vehicle_type
                    FROM reservations r
                    JOIN vehicles v ON r.vehicle_id = v.vehicle_id
                    JOIN slots s ON s.slot_id=r.slot_id
                    WHERE r.reservation_id = %s
                    FOR UPDATE OF r;
                """, (res_id,))
                res = cur.fetchone()
                if not res:
                    return jsonify({"status": "error", "message": "Không tìm thấy mã đặt chỗ này."}), 404

                if user["role"] == "customer" and res["user_id"] != user["user_id"]:
                    return jsonify({"status": "error", "message": "Bạn chỉ được thanh toán cho giao dịch của mình."}), 403

                if res["payment_status"] != "Pending":
                    return jsonify({"status": "error", "message": "Mã đặt chỗ này không còn ở trạng thái chờ thanh toán."}), 409
                if res["vehicle_departed_at"] is None:
                    return jsonify({"status": "error", "message": "Hãy xác nhận đã rời chỗ trước khi thanh toán."}), 409
                if res["start_time"] > datetime.now(APP_TIMEZONE):
                    return jsonify({"status": "error", "message": "Chưa đến giờ bắt đầu đặt chỗ nên chưa thể thanh toán."}), 409
                cur.execute("SELECT COALESCE(SUM(ROUND(ss.energy_consumed_kwh * cr.rate_per_kwh, 0)), 0) AS amount, COUNT(*) AS completed_sessions FROM charging_sessions ss JOIN charging_rates cr ON cr.rate_id=ss.rate_id WHERE ss.reservation_id=%s AND ss.session_status='Completed';", (res_id,))
                charging = cur.fetchone()
                if (res["slot_type"] == "Charging"
                        and res["vehicle_type"] == "EV"
                        and charging["completed_sessions"] == 0):
                    return jsonify({"status": "error", "message": "Mã đặt chỗ có sạc điện chỉ được thanh toán sau khi kết thúc phiên sạc."}), 409
                cur.execute("SELECT COALESCE(SUM(amount), 0) AS amount FROM fees WHERE reservation_id=%s;", (res_id,))
                violation = cur.fetchone()
                parking_cost = float(res["parking_cost"] or 0)
                charging_cost = float(charging["amount"] or 0)
                violation_cost = float(violation["amount"] or 0)
                total_cost = parking_cost + charging_cost + violation_cost

                cur.execute("""
                    INSERT INTO payments (payment_id, reservation_id, amount, payment_method, payment_status)
                    VALUES (%s, %s, %s, %s, 'Completed');
                """, (pay_id, res_id, total_cost, encrypted_method))

                cur.execute("UPDATE reservations SET payment_status = 'Paid' WHERE reservation_id = %s;", (res_id,))
            conn.commit()
        return jsonify({
            "status": "success",
            "message": f"Thanh toán thành công {int(total_cost):,} VNĐ cho mã đặt chỗ {res_id}."
        }), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/rates", methods=["GET", "POST"])
@admin_required
def api_rates():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if request.method == "GET":
                    cur.execute("""
                        SELECT p.rate_id, p.location_id, p.service_type, p.rate_type,
                               to_char(p.effective_from, 'HH24:MI:SS') AS effective_from,
                               to_char(p.effective_to, 'HH24:MI:SS') AS effective_to,
                               p.price_per_hour, pl.location_name, TRUE AS is_current,
                               p.service_type AS kind, p.price_per_hour AS amount,
                               (SELECT c.rate_id FROM charging_rates c
                                WHERE c.location_id=p.location_id AND c.rate_type=p.rate_type
                                  AND c.effective_from=p.effective_from AND c.effective_to=p.effective_to
                                ORDER BY COALESCE(NULLIF(substring(c.rate_id from '[0-9]+$'), ''), '0')::BIGINT DESC LIMIT 1) AS charging_rate_id,
                               (SELECT c.rate_per_kwh FROM charging_rates c
                                WHERE c.location_id=p.location_id AND c.rate_type=p.rate_type
                                  AND c.effective_from=p.effective_from AND c.effective_to=p.effective_to
                                ORDER BY COALESCE(NULLIF(substring(c.rate_id from '[0-9]+$'), ''), '0')::BIGINT DESC LIMIT 1) AS charging_amount
                        FROM parking_rates p JOIN parking_locations pl ON pl.location_id=p.location_id
                        WHERE NOT EXISTS (
                            SELECT 1 FROM parking_rates newer
                            WHERE newer.location_id=p.location_id AND newer.service_type=p.service_type
                              AND newer.rate_type=p.rate_type
                              AND COALESCE(NULLIF(substring(newer.rate_id from '[0-9]+$'), ''), '0')::BIGINT
                                  > COALESCE(NULLIF(substring(p.rate_id from '[0-9]+$'), ''), '0')::BIGINT
                        )
                        ORDER BY pl.location_name, p.rate_type;
                    """)
                    parking = cur.fetchall()
                    cur.execute("""
                        SELECT v.*, pl.location_name, 'violation' AS kind,
                               'Phí vi phạm' AS rate_type, TRUE AS is_current
                        FROM violation_rates v
                        JOIN parking_locations pl ON pl.location_id=v.location_id
                        WHERE NOT EXISTS (
                            SELECT 1 FROM violation_rates newer
                            WHERE newer.location_id=v.location_id
                              AND COALESCE(NULLIF(substring(newer.rate_id from '[0-9]+$'), ''), '0')::BIGINT
                                  > COALESCE(NULLIF(substring(v.rate_id from '[0-9]+$'), ''), '0')::BIGINT
                        )
                        ORDER BY pl.location_name;
                    """)
                    violation = cur.fetchall()
                    locks = locked_location_ids(cur)
                    for rate in parking + violation:
                        rate["lock_reason"] = locks.get(rate["location_id"])
                        rate["is_locked"] = bool(rate["lock_reason"])
                    return jsonify({"status": "success", "data": parking + violation}), 200

                data = request.get_json(force=True)
                try:
                    location_id = (data.get("location_id") or "").strip()
                    kind = data.get("kind")
                    if kind not in ("parking", "both", "violation"):
                        raise ValueError("Chỉ có thể thêm giá đỗ xe, giá đỗ xe và sạc điện hoặc phí vi phạm.")
                    cur.execute("SELECT 1 FROM parking_locations WHERE location_id=%s AND status='Active';", (location_id,))
                    if not cur.fetchone(): raise ValueError("Hãy chọn một bãi đỗ đang hoạt động.")
                    if kind == "violation":
                        price = read_number(data.get("value", data.get("violation_value")), "Phí vi phạm")
                    else:
                        rate_type, start, end = data.get("rate_type"), None, None
                        if rate_type not in ("Peak", "Normal", "Off-Peak"): raise ValueError("Khung giá không hợp lệ.")
                        try:
                            start = datetime.strptime(data.get("effective_from", ""), "%H:%M").time()
                            end = datetime.strptime(data.get("effective_to", ""), "%H:%M").time()
                        except (TypeError, ValueError): raise ValueError("Vui lòng nhập giờ bắt đầu và kết thúc hợp lệ.")
                        if start == end: raise ValueError("Giờ kết thúc phải khác giờ bắt đầu.")
                        price = read_number(data.get("parking_value", data.get("value")), "Giá đỗ xe")
                        charging_price = read_number(data.get("charging_value"), "Giá sạc") if kind == "both" else None
                        ensure_rate_interval_available(cur, "parking", rate_type, start, end, location_id, kind)
                        if kind == "both": ensure_rate_interval_available(cur, "charging", rate_type, start, end, location_id)
                except ValueError as e:
                    return jsonify({"status": "error", "message": str(e)}), 400
                if kind == "violation":
                    rate_id = generate_db_id(cur, "violation_rate")
                    cur.execute("INSERT INTO violation_rates (rate_id, location_id, amount) VALUES (%s, %s, %s);", (rate_id, location_id, price))
                else:
                    rate_id = generate_db_id(cur, "parking_rate")
                    cur.execute("INSERT INTO parking_rates (rate_id, location_id, service_type, rate_type, effective_from, effective_to, price_per_hour) VALUES (%s, %s, %s, %s, %s, %s, %s);", (rate_id, location_id, kind, rate_type, start, end, price))
                    if kind == "both":
                        charging_id = generate_db_id(cur, "charging_rate")
                        cur.execute("INSERT INTO charging_rates (rate_id, location_id, rate_per_kwh, effective_from, effective_to, rate_type) VALUES (%s, %s, %s, %s, %s, %s);", (charging_id, location_id, charging_price, start, end, rate_type))
            conn.commit()
        return jsonify({"status": "success", "message": f"Đã thêm mức giá: {rate_id}.", "rate_id": rate_id}), 201
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/rates/<rate_id>", methods=["PUT", "DELETE"])
@admin_required
def api_rate_detail(rate_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                data = request.get_json(force=True) if request.method == "PUT" else {}
                kind = data.get("kind") if data else None
                candidates = [("parking", "parking_rates", "price_per_hour"), ("charging", "charging_rates", "rate_per_kwh"), ("violation", "violation_rates", "amount")]
                lookup_kind = "parking" if kind == "both" else kind
                if lookup_kind in ("parking", "charging", "violation"):
                    candidates = [entry for entry in candidates if entry[0] == lookup_kind]
                found = None
                for candidate_kind, table, price_column in candidates:
                    cur.execute(f"SELECT * FROM {table} WHERE rate_id = %s FOR UPDATE;", (rate_id,))
                    row = cur.fetchone()
                    if row:
                        found = (candidate_kind, table, price_column, row)
                        break
                if not found:
                    return jsonify({"status": "error", "message": "Không tìm thấy mức giá."}), 404
                found_kind, table, price_column, old_rate = found
                lock_reason = location_lock_reason(cur, old_rate["location_id"])
                if lock_reason:
                    return jsonify({"status": "error", "message": f"Không thể sửa hoặc xóa biểu giá. {lock_reason}"}), 409
                if request.method == "DELETE":
                    paired_charging = None
                    if found_kind == "parking" and old_rate.get("service_type") == "both":
                        cur.execute("SELECT * FROM charging_rates WHERE location_id=%s AND rate_type=%s AND effective_from=%s AND effective_to=%s ORDER BY COALESCE(NULLIF(substring(rate_id from '[0-9]+$'), ''), '0')::BIGINT DESC LIMIT 1 FOR UPDATE;", (old_rate["location_id"], old_rate["rate_type"], old_rate["effective_from"], old_rate["effective_to"]))
                        paired_charging = cur.fetchone()
                    if found_kind == "violation":
                        cur.execute("SELECT EXISTS (SELECT 1 FROM fees WHERE fee_type='Violation' AND reservation_id IN (SELECT reservation_id FROM reservations)) AS is_used;")
                    else:
                        reference_table = "reservations" if found_kind == "parking" else "charging_sessions"
                        cur.execute(f"SELECT EXISTS (SELECT 1 FROM {reference_table} WHERE rate_id = %s) AS is_used;", (rate_id,))
                    used = cur.fetchone()["is_used"]
                    if paired_charging:
                        cur.execute("SELECT EXISTS (SELECT 1 FROM charging_sessions WHERE rate_id=%s) AS is_used;", (paired_charging["rate_id"],))
                        used = used or cur.fetchone()["is_used"]
                    if used:
                        return jsonify({"status": "error", "message": "Mức giá đã được dùng trong giao dịch nên không thể xóa để giữ lịch sử."}), 409
                    if paired_charging:
                        cur.execute("DELETE FROM charging_rates WHERE rate_id=%s;", (paired_charging["rate_id"],))
                    cur.execute(f"DELETE FROM {table} WHERE rate_id = %s;", (rate_id,))
                    message = "Đã xóa mức giá."
                else:
                    if kind == "both":
                        if found_kind != "parking" or old_rate.get("service_type") != "both":
                            return jsonify({"status": "error", "message": "Mức giá này không thuộc dịch vụ Đỗ xe và sạc điện."}), 400
                        try:
                            rate_type = data.get("rate_type")
                            if rate_type not in ("Peak", "Normal", "Off-Peak"):
                                raise ValueError("Khung giá không hợp lệ.")
                            start = datetime.strptime(data.get("effective_from", ""), "%H:%M").time()
                            end = datetime.strptime(data.get("effective_to", ""), "%H:%M").time()
                            if start == end:
                                raise ValueError("Giờ kết thúc phải khác giờ bắt đầu.")
                            parking_price = read_number(data.get("parking_value"), "Giá đỗ xe")
                            charging_price = read_number(data.get("charging_value"), "Giá sạc")
                            cur.execute("SELECT * FROM charging_rates WHERE location_id=%s AND rate_type=%s AND effective_from=%s AND effective_to=%s ORDER BY COALESCE(NULLIF(substring(rate_id from '[0-9]+$'), ''), '0')::BIGINT DESC LIMIT 1 FOR UPDATE;", (old_rate["location_id"], old_rate["rate_type"], old_rate["effective_from"], old_rate["effective_to"]))
                            paired_charging = cur.fetchone()
                            if not paired_charging:
                                raise ValueError("Không tìm thấy phần giá sạc đang ghép với mức giá này.")
                            ensure_rate_interval_available(cur, "parking", rate_type, start, end, old_rate["location_id"], "both", (rate_id,))
                            ensure_rate_interval_available(cur, "charging", rate_type, start, end, old_rate["location_id"], exclude_rate_ids=(paired_charging["rate_id"],))
                        except (TypeError, ValueError) as e:
                            return jsonify({"status": "error", "message": str(e) or "Thông tin mức giá không hợp lệ."}), 400
                        cur.execute("SELECT EXISTS (SELECT 1 FROM reservations WHERE rate_id=%s) AS used;", (rate_id,))
                        parking_used = cur.fetchone()["used"]
                        cur.execute("SELECT EXISTS (SELECT 1 FROM charging_sessions WHERE rate_id=%s) AS used;", (paired_charging["rate_id"],))
                        charging_used = cur.fetchone()["used"]
                        if parking_used or charging_used:
                            new_parking_id = generate_db_id(cur, "parking_rate")
                            new_charging_id = generate_db_id(cur, "charging_rate")
                            cur.execute("INSERT INTO parking_rates (rate_id, location_id, service_type, rate_type, effective_from, effective_to, price_per_hour) VALUES (%s, %s, 'both', %s, %s, %s, %s);", (new_parking_id, old_rate["location_id"], rate_type, start, end, parking_price))
                            cur.execute("INSERT INTO charging_rates (rate_id, location_id, rate_per_kwh, effective_from, effective_to, rate_type) VALUES (%s, %s, %s, %s, %s, %s);", (new_charging_id, old_rate["location_id"], charging_price, start, end, rate_type))
                            message = f"Đã tạo phiên bản mới {new_parking_id}; lịch sử giao dịch cũ được giữ nguyên."
                        else:
                            cur.execute("UPDATE parking_rates SET rate_type=%s, effective_from=%s, effective_to=%s, price_per_hour=%s WHERE rate_id=%s;", (rate_type, start, end, parking_price, rate_id))
                            cur.execute("UPDATE charging_rates SET rate_type=%s, effective_from=%s, effective_to=%s, rate_per_kwh=%s WHERE rate_id=%s;", (rate_type, start, end, charging_price, paired_charging["rate_id"]))
                            message = "Đã cập nhật mức giá đỗ xe và sạc điện."
                    elif kind != found_kind:
                        return jsonify({"status": "error", "message": "Loại dịch vụ không khớp với mức giá cần sửa."}), 400
                    else:
                      try:
                        if found_kind == "violation":
                            price = read_number(data.get("value"), "Phí vi phạm")
                            rate_type = start = end = None
                        else:
                            rate_type = data.get("rate_type")
                            if rate_type not in ("Peak", "Normal", "Off-Peak"):
                                raise ValueError("Khung giá không hợp lệ.")
                            start = datetime.strptime(data.get("effective_from", ""), "%H:%M").time()
                            end = datetime.strptime(data.get("effective_to", ""), "%H:%M").time()
                            if start == end:
                                raise ValueError("Giờ kết thúc phải khác giờ bắt đầu.")
                            price = read_number(data.get("value"), "Đơn giá")
                            ensure_rate_interval_available(cur, found_kind, rate_type, start, end, old_rate["location_id"], old_rate.get("service_type"), (rate_id,))
                      except (TypeError, ValueError) as e:
                        return jsonify({"status": "error", "message": str(e) or "Thông tin mức giá không hợp lệ."}), 400

                      if found_kind == "violation":
                        cur.execute("UPDATE violation_rates SET amount=%s, created_at=CURRENT_TIMESTAMP WHERE rate_id=%s;", (price, rate_id))
                        message = "Đã cập nhật phí vi phạm."
                      else:
                        reference_table = "reservations" if found_kind == "parking" else "charging_sessions"
                        cur.execute(f"SELECT EXISTS (SELECT 1 FROM {reference_table} WHERE rate_id=%s) AS is_used;", (rate_id,))
                        if cur.fetchone()["is_used"]:
                            new_rate_id = generate_db_id(cur, "parking_rate" if found_kind == "parking" else "charging_rate")
                            if found_kind == "parking":
                                cur.execute("INSERT INTO parking_rates (rate_id, location_id, service_type, rate_type, effective_from, effective_to, price_per_hour) VALUES (%s, %s, %s, %s, %s, %s, %s);", (new_rate_id, old_rate["location_id"], old_rate.get("service_type", "parking"), rate_type, start, end, price))
                            else:
                                cur.execute("INSERT INTO charging_rates (rate_id, location_id, rate_per_kwh, effective_from, effective_to, rate_type) VALUES (%s, %s, %s, %s, %s, %s);", (new_rate_id, old_rate["location_id"], price, start, end, rate_type))
                            message = f"Đã lưu mức giá mới {new_rate_id}; lịch sử giao dịch cũ được giữ nguyên."
                        else:
                            if found_kind == "parking":
                                cur.execute("UPDATE parking_rates SET rate_type=%s, effective_from=%s, effective_to=%s, price_per_hour=%s WHERE rate_id=%s;", (rate_type, start, end, price, rate_id))
                            else:
                                cur.execute("UPDATE charging_rates SET rate_type=%s, effective_from=%s, effective_to=%s, rate_per_kwh=%s WHERE rate_id=%s;", (rate_type, start, end, price, rate_id))
                            message = "Đã cập nhật mức giá."
            conn.commit()
        return jsonify({"status": "success", "message": message}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


# ==============================================================================
# PHỤC VỤ GIAO DIỆN TÁCH RIÊNG VÀ KHỞI ĐỘNG ỨNG DỤNG
# ==============================================================================
FRONTEND_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend.html")


@app.route("/", methods=["GET"])
def home_page():
    """Trả giao diện; dữ liệu động được tải qua các API ở phía backend."""
    return send_file(FRONTEND_PATH, mimetype="text/html")


@app.errorhandler(404)
def page_not_found(e):
    """Trả thông báo gọn khi đường dẫn không tồn tại."""
    return "Không tìm thấy trang yêu cầu trên hệ thống Smart City.", 404


if __name__ == "__main__":
    # Tác vụ nền tự hoàn tất phiên sạc quá hạn, chạy cùng ứng dụng Flask.
    threading.Thread(target=charging_session_watchdog, daemon=True, name="charging-session-watchdog").start()
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
