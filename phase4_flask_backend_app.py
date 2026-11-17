# -*- coding: utf-8 -*-
"""
HỆ THỐNG SMART CITY – QUẢN LÝ TRẠM SẠC & BÃI ĐỖ XE THÔNG MINH
Backend Python Flask + Giao diện Tiếng Việt 100% (Phong cách Dark Emerald)
"""

import os
import hashlib
import time
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, jsonify, request, session, render_template_string
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "smart_city_emerald_secret_2026")

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


def is_valid_gmail(email_str: str) -> bool:
    """Kiểm tra email bắt buộc có đuôi @gmail.com."""
    e = (email_str or "").strip().lower()
    return len(e) > 10 and e.endswith("@gmail.com") and " " not in e


def calculate_dynamic_parking_fee(start_dt: datetime, end_dt: datetime, rates_list: list) -> float:
    """
    Tính phí đỗ xe theo giá động: chia theo từng khung giờ cao điểm / bình thường / thấp điểm.
    """
    rate_map = {"Peak": 45000.0, "Normal": 25000.0, "Off-Peak": 15000.0}
    for r in rates_list:
        rate_map[r["rate_type"]] = float(r["price_per_hour"])

    total_fee = 0.0
    cur = start_dt
    step = timedelta(minutes=15)
    while cur < end_dt:
        nxt = min(cur + step, end_dt)
        hours = (nxt - cur).total_seconds() / 3600.0
        h = cur.hour
        if 17 <= h < 22:
            tier_price = rate_map.get("Peak", 45000.0)
        elif 6 <= h < 17:
            tier_price = rate_map.get("Normal", 25000.0)
        else:
            tier_price = rate_map.get("Off-Peak", 15000.0)
        total_fee += hours * tier_price
        cur = nxt
    return round(total_fee, 0)


def ensure_schema_ready():
    """Đồng bộ cấu trúc bảng users (ràng buộc @gmail.com và 2 vai trò RBAC) khi khởi động."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    ALTER TABLE IF EXISTS users
                    ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'customer';
                """)
                default_pw = hash_text("123456")
                cur.execute("""
                    UPDATE users SET email = 'quantri.smartcity@gmail.com', role = 'system_admin', password_hash = %s WHERE user_id = 'USR001';
                    UPDATE users SET email = 'thaithien.sc@gmail.com', role = 'customer', password_hash = %s WHERE user_id = 'USR002';
                    UPDATE users SET email = 'thanhtam.sc@gmail.com', role = 'customer', password_hash = %s WHERE user_id = 'USR003';
                    UPDATE users SET email = 'hoangminh.sc@gmail.com', role = 'customer', password_hash = %s WHERE user_id = 'USR004';
                    UPDATE users SET email = 'maianh.sc@gmail.com', role = 'customer', password_hash = %s WHERE user_id = 'USR005';
                """, (default_pw, default_pw, default_pw, default_pw, default_pw))
                cur.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = 'chk_users_email_gmail'
                        ) THEN
                            ALTER TABLE users ADD CONSTRAINT chk_users_email_gmail CHECK (LOWER(email) LIKE '%_@gmail.com');
                        END IF;
                    END $$;
                """)
            conn.commit()
    except Exception:
        pass


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
# API CHỨC NĂNG (PHÂN QUYỀN 2 VAI TRÒ: QUẢN TRỊ HỆ THỐNG & KHÁCH HÀNG)
# ==============================================================================
@app.route("/api/overview", methods=["GET"])
@admin_required
def api_overview():
    start_ts = time.perf_counter()
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS cnt FROM slots WHERE status = 'Available';")
                avail_slots = cur.fetchone()["cnt"]

                cur.execute("SELECT COUNT(*) AS cnt FROM charging_stations WHERE status = 'Online';")
                online_stations = cur.fetchone()["cnt"]

                cur.execute("SELECT COUNT(*) AS cnt FROM vw_active_reservations;")
                active_res = cur.fetchone()["cnt"]

                cur.execute("SELECT COALESCE(SUM(amount), 0) AS rev FROM payments WHERE payment_status = 'Completed';")
                total_rev = float(cur.fetchone()["rev"])

                cur.execute("SELECT * FROM vw_active_reservations ORDER BY start_time DESC;")
                active_list = cur.fetchall()

                # Truy vấn INNER JOIN 5 bảng phát hiện xe ICE đỗ ở chỗ sạc điện
                cur.execute("""
                    SELECT r.reservation_id, u.name AS owner_name, v.plate_number,
                           s.slot_id, cs.violation_fee_rate, r.start_time, r.end_time, r.total_amount
                    FROM vehicles v
                    INNER JOIN users u ON v.user_id = u.user_id
                    INNER JOIN reservations r ON v.vehicle_id = r.vehicle_id
                    INNER JOIN slots s ON r.slot_id = s.slot_id
                    INNER JOIN charging_slots cs ON s.slot_id = cs.slot_id
                    WHERE v.vehicle_type = 'ICE' AND s.slot_type = 'Charging'
                    ORDER BY r.start_time DESC;
                """)
                violations = cur.fetchall()

        elapsed_ms = round((time.perf_counter() - start_ts) * 1000, 2)
        return jsonify({
            "status": "success",
            "query_time_ms": elapsed_ms,
            "kpis": {
                "avail_slots": avail_slots,
                "online_stations": online_stations,
                "active_res": active_res,
                "total_rev": total_rev,
            },
            "active_reservations": active_list,
            "violations": violations,
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
                        SELECT u.user_id, u.name, u.email, u.phone_number, u.role,
                               COUNT(v.vehicle_id) AS vehicle_count
                        FROM users u
                        LEFT JOIN vehicles v ON u.user_id = v.user_id
                        GROUP BY u.user_id, u.name, u.email, u.phone_number, u.role
                        ORDER BY u.user_id;
                    """)
                    return jsonify({"status": "success", "data": cur.fetchall()}), 200

                data = request.get_json(force=True)
                email = (data.get("email") or "").strip().lower()
                if not is_valid_gmail(email):
                    return jsonify({"status": "error", "message": "Email phải có đuôi @gmail.com"}), 400

                cur.execute(
                    "INSERT INTO users (user_id, name, email, phone_number, password_hash, role) VALUES (%s, %s, %s, %s, %s, 'customer');",
                    (
                        data.get("user_id"),
                        data.get("name"),
                        email,
                        data.get("phone_number"),
                        hash_text(data.get("password", "123456")),
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
                cur.execute("DELETE FROM users WHERE user_id = %s;", (user_id,))
            conn.commit()
        return jsonify({"status": "success", "message": "Đã xóa người dùng và các phương tiện liên kết."}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/vehicles", methods=["GET", "POST"])
@login_required
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


@app.route("/api/stations", methods=["GET"])
@admin_required
def api_stations():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT st.station_id, st.slot_id, st.connector_type, st.max_power_kw, st.status,
                           cs.charger_type, cs.kw_rate, cs.power_capacity, cs.idle_fee_rate, cs.violation_fee_rate,
                           pl.location_name, s.floor
                    FROM charging_stations st
                    JOIN charging_slots cs ON st.slot_id = cs.slot_id
                    JOIN slots s ON cs.slot_id = s.slot_id
                    JOIN parking_locations pl ON s.location_id = pl.location_id
                    ORDER BY st.station_id;
                """)
                return jsonify({"status": "success", "data": cur.fetchall()}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/slots", methods=["GET"])
@login_required
def api_slots():
    t0 = time.perf_counter()
    location_id = request.args.get("location_id")
    slot_type = request.args.get("slot_type")
    only_available = request.args.get("only_available") == "true"

    sql = """
        SELECT s.slot_id, s.location_id, pl.location_name, pl.address,
               s.floor, s.slot_type, s.status,
               cs.charger_type, cs.kw_rate, cs.idle_fee_rate, cs.violation_fee_rate,
               st.station_id, st.connector_type, st.max_power_kw
        FROM slots s
        JOIN parking_locations pl ON s.location_id = pl.location_id
        LEFT JOIN charging_slots cs ON s.slot_id = cs.slot_id
        LEFT JOIN charging_stations st ON cs.slot_id = st.slot_id
        WHERE (%s IS NULL OR %s = '' OR s.location_id = %s)
          AND (%s IS NULL OR %s = '' OR s.slot_type = %s)
          AND (%s = FALSE OR s.status = 'Available')
        ORDER BY s.slot_id;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (location_id, location_id, location_id, slot_type, slot_type, slot_type, only_available))
                rows = cur.fetchall()
        ms = round((time.perf_counter() - t0) * 1000, 2)
        return jsonify({"status": "success", "query_time_ms": ms, "data": rows}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/slots/<slot_id>/status", methods=["PUT"])
@admin_required
def api_update_slot_status(slot_id):
    data = request.get_json(force=True)
    new_status = data.get("status")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE slots SET status = %s WHERE slot_id = %s;", (new_status, slot_id))
            conn.commit()
        return jsonify({"status": "success", "message": f"Đã cập nhật trạng thái chỗ đỗ {slot_id}."}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/reservations", methods=["GET", "POST"])
@login_required
def api_reservations():
    user = session["user"]
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if request.method == "GET":
                    if user["role"] == "customer":
                        cur.execute("""
                            SELECT r.*, v.plate_number, v.vehicle_type, u.name AS customer_name, s.slot_type
                            FROM reservations r
                            JOIN vehicles v ON r.vehicle_id = v.vehicle_id
                            JOIN users u ON v.user_id = u.user_id
                            JOIN slots s ON r.slot_id = s.slot_id
                            WHERE u.user_id = %s
                            ORDER BY r.start_time DESC;
                        """, (user["user_id"],))
                    else:
                        cur.execute("""
                            SELECT r.*, v.plate_number, v.vehicle_type, u.name AS customer_name, s.slot_type
                            FROM reservations r
                            JOIN vehicles v ON r.vehicle_id = v.vehicle_id
                            JOIN users u ON v.user_id = u.user_id
                            JOIN slots s ON r.slot_id = s.slot_id
                            ORDER BY r.start_time DESC;
                        """)
                    return jsonify({"status": "success", "data": cur.fetchall()}), 200

                data = request.get_json(force=True)
                res_id = (data.get("reservation_id") or "").strip() or ("RES" + str(int(time.time()) % 1000000))
                vehicle_id = data.get("vehicle_id")
                slot_id = data.get("slot_id")
                rate_id = data.get("rate_id") or "PR_NORMAL"
                start_str = (data.get("start_time") or "").replace("T", " ")
                end_str = (data.get("end_time") or "").replace("T", " ")

                if user["role"] == "customer":
                    cur.execute("SELECT user_id, vehicle_type FROM vehicles WHERE vehicle_id = %s;", (vehicle_id,))
                    v_check = cur.fetchone()
                    if not v_check or v_check["user_id"] != user["user_id"]:
                        return jsonify({"status": "error", "message": "Bạn chỉ được đặt chỗ cho xe của mình."}), 403
                    veh_row = v_check
                else:
                    cur.execute("SELECT vehicle_type FROM vehicles WHERE vehicle_id = %s;", (vehicle_id,))
                    veh_row = cur.fetchone()

                cur.execute("SELECT * FROM parking_rates;")
                rates_list = cur.fetchall()

                try:
                    start_dt = datetime.fromisoformat(start_str)
                    end_dt = datetime.fromisoformat(end_str)
                except ValueError:
                    return jsonify({"status": "error", "message": "Thời gian nhập vào không hợp lệ."}), 400

                dynamic_parking_fee = 0.0
                if end_dt > start_dt:
                    dynamic_parking_fee = calculate_dynamic_parking_fee(start_dt, end_dt, rates_list)

                cur.execute("SELECT slot_type FROM slots WHERE slot_id = %s;", (slot_id,))
                slot_row = cur.fetchone()

                violation_fee = 0.0
                if veh_row and slot_row and veh_row["vehicle_type"] == "ICE" and slot_row["slot_type"] == "Charging":
                    cur.execute("SELECT violation_fee_rate FROM charging_slots WHERE slot_id = %s;", (slot_id,))
                    cs_row = cur.fetchone()
                    violation_fee = float(cs_row["violation_fee_rate"]) if cs_row else 200000.0

                total_initial = dynamic_parking_fee + violation_fee

                cur.execute("""
                    INSERT INTO reservations (reservation_id, vehicle_id, slot_id, rate_id, start_time, end_time, total_amount, payment_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'Pending');
                """, (res_id, vehicle_id, slot_id, rate_id, start_str, end_str, total_initial))

                if violation_fee > 0:
                    fee_id = "FEE" + str(int(time.time()) % 100000)
                    duration_mins = max(0, int((end_dt - start_dt).total_seconds() / 60))
                    cur.execute("""
                        INSERT INTO fees (fee_id, reservation_id, fee_type, amount, excess_duration)
                        VALUES (%s, %s, 'Violation', %s, %s);
                    """, (fee_id, res_id, violation_fee, duration_mins))

                cur.execute("UPDATE slots SET status = 'Reserved' WHERE slot_id = %s;", (slot_id,))
            conn.commit()

        msg = f"Đặt chỗ thành công. Phí đỗ xe tạm tính: {int(dynamic_parking_fee):,} VNĐ."
        if violation_fee > 0:
            msg += f" Xe xăng đỗ tại chỗ sạc điện bị áp phí vi phạm: {int(violation_fee):,} VNĐ."
        return jsonify({"status": "success", "message": msg}), 201
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/sessions", methods=["GET", "POST"])
@login_required
def api_sessions():
    """
    Ghi nhận phiên sạc:
    energy_consumed_kwh do HỆ THỐNG TỰ ĐỘNG TÍNH từ công suất trạm sạc và thời gian sạc,
    API hoàn toàn KHÔNG nhận giá trị kWh từ phía người dùng.
    """
    user = session["user"]
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if request.method == "GET":
                    if user["role"] == "customer":
                        cur.execute("""
                            SELECT ss.*, v.plate_number, cr.rate_type, cr.rate_per_kwh,
                                   ROUND(ss.energy_consumed_kwh * cr.rate_per_kwh, 0) AS charging_cost
                            FROM charging_sessions ss
                            JOIN vehicles v ON ss.vehicle_id = v.vehicle_id
                            JOIN charging_rates cr ON ss.rate_id = cr.rate_id
                            WHERE v.user_id = %s
                            ORDER BY ss.start_date_time DESC;
                        """, (user["user_id"],))
                    else:
                        cur.execute("""
                            SELECT ss.*, v.plate_number, cr.rate_type, cr.rate_per_kwh,
                                   ROUND(ss.energy_consumed_kwh * cr.rate_per_kwh, 0) AS charging_cost
                            FROM charging_sessions ss
                            JOIN vehicles v ON ss.vehicle_id = v.vehicle_id
                            JOIN charging_rates cr ON ss.rate_id = cr.rate_id
                            ORDER BY ss.start_date_time DESC;
                        """)
                    return jsonify({"status": "success", "data": cur.fetchall()}), 200

                data = request.get_json(force=True)
                ses_id = "SES" + str(int(time.time()) % 100000)
                vehicle_id = data.get("vehicle_id")
                station_id = data.get("charging_station_id")
                rate_id = data.get("rate_id") or "CR_NORMAL"
                start_str = (data.get("start_date_time") or "").replace("T", " ")
                end_str = (data.get("end_date_time") or "").replace("T", " ")
                reservation_id = (data.get("reservation_id") or "").strip()

                try:
                    start_dt = datetime.fromisoformat(start_str)
                    end_dt = datetime.fromisoformat(end_str)
                except ValueError:
                    return jsonify({"status": "error", "message": "Thời gian phiên sạc không hợp lệ."}), 400

                if end_dt <= start_dt:
                    return jsonify({"status": "error", "message": "Thời gian kết thúc phải lớn hơn thời gian bắt đầu."}), 400

                # Hệ thống tự động đo đếm điện năng tiêu thụ (kWh) dựa trên công suất trụ sạc và thời lượng sạc
                cur.execute("""
                    SELECT st.max_power_kw, cs.kw_rate
                    FROM charging_stations st
                    JOIN charging_slots cs ON st.slot_id = cs.slot_id
                    WHERE st.station_id = %s;
                """, (station_id,))
                st_row = cur.fetchone()
                effective_kw = float(st_row["kw_rate"] if st_row else 22.0)
                duration_hours = (end_dt - start_dt).total_seconds() / 3600.0
                # Hệ thống tự động tính số điện tiêu thụ (hiệu suất nạp thực tế 85% công suất định mức)
                auto_energy_kwh = round(max(0.5, duration_hours * effective_kw * 0.85), 2)

                cur.execute("""
                    INSERT INTO charging_sessions (session_id, vehicle_id, charging_station_id, rate_id, start_date_time, end_date_time, energy_consumed_kwh)
                    VALUES (%s, %s, %s, %s, %s, %s, %s);
                """, (ses_id, vehicle_id, station_id, rate_id, start_str, end_str, auto_energy_kwh))

                cur.execute("SELECT rate_per_kwh FROM charging_rates WHERE rate_id = %s;", (rate_id,))
                cr_row = cur.fetchone()
                rate_kwh = float(cr_row["rate_per_kwh"]) if cr_row else 3500.0
                charging_cost = round(auto_energy_kwh * rate_kwh, 0)

                idle_fee_added = 0.0
                if reservation_id:
                    cur.execute("""
                        SELECT r.end_time, r.slot_id, cs.idle_fee_rate
                        FROM reservations r
                        LEFT JOIN charging_slots cs ON r.slot_id = cs.slot_id
                        WHERE r.reservation_id = %s;
                    """, (reservation_id,))
                    res_row = cur.fetchone()
                    if res_row:
                        res_end_dt = res_row["end_time"]
                        idle_mins = int((res_end_dt - end_dt).total_seconds() / 60)
                        if idle_mins > 15:
                            idle_rate = float(res_row["idle_fee_rate"] or 50000.0)
                            idle_fee_added = round((idle_mins / 60.0) * idle_rate, 0)
                            fee_id = "FEE" + str(int(time.time()) % 100000)
                            cur.execute("""
                                INSERT INTO fees (fee_id, reservation_id, fee_type, amount, excess_duration)
                                VALUES (%s, %s, 'Idle', %s, %s);
                            """, (fee_id, reservation_id, idle_fee_added, idle_mins))

                        cur.execute("""
                            UPDATE reservations
                            SET total_amount = COALESCE(total_amount, 0) + %s + %s
                            WHERE reservation_id = %s;
                        """, (charging_cost, idle_fee_added, reservation_id))
            conn.commit()

        msg = f"Hệ thống đã tự động ghi nhận {auto_energy_kwh} kWh (Phí sạc: {int(charging_cost):,} VNĐ)."
        if idle_fee_added > 0:
            msg += f" Phí chiếm chỗ sau khi sạc: {int(idle_fee_added):,} VNĐ."
        return jsonify({"status": "success", "message": msg}), 201
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/billing", methods=["GET"])
@login_required
def api_billing():
    user = session["user"]
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if user["role"] == "customer":
                    cur.execute("""
                        SELECT f.*, v.plate_number, u.name AS customer_name
                        FROM fees f
                        JOIN reservations r ON f.reservation_id = r.reservation_id
                        JOIN vehicles v ON r.vehicle_id = v.vehicle_id
                        JOIN users u ON v.user_id = u.user_id
                        WHERE u.user_id = %s
                        ORDER BY f.fee_id DESC;
                    """, (user["user_id"],))
                    fees_list = cur.fetchall()

                    cur.execute("""
                        SELECT p.*, r.vehicle_id, v.plate_number, u.name AS customer_name
                        FROM payments p
                        JOIN reservations r ON p.reservation_id = r.reservation_id
                        JOIN vehicles v ON r.vehicle_id = v.vehicle_id
                        JOIN users u ON v.user_id = u.user_id
                        WHERE u.user_id = %s
                        ORDER BY p.payment_datetime DESC;
                    """, (user["user_id"],))
                    payments_list = cur.fetchall()
                else:
                    cur.execute("""
                        SELECT f.*, v.plate_number, u.name AS customer_name
                        FROM fees f
                        JOIN reservations r ON f.reservation_id = r.reservation_id
                        JOIN vehicles v ON r.vehicle_id = v.vehicle_id
                        JOIN users u ON v.user_id = u.user_id
                        ORDER BY f.fee_id DESC;
                    """)
                    fees_list = cur.fetchall()

                    cur.execute("""
                        SELECT p.*, r.vehicle_id, v.plate_number, u.name AS customer_name
                        FROM payments p
                        JOIN reservations r ON p.reservation_id = r.reservation_id
                        JOIN vehicles v ON r.vehicle_id = v.vehicle_id
                        JOIN users u ON v.user_id = u.user_id
                        ORDER BY p.payment_datetime DESC;
                    """)
                    payments_list = cur.fetchall()

                cur.execute("SELECT * FROM parking_rates ORDER BY price_per_hour;")
                p_rates = cur.fetchall()
                cur.execute("SELECT * FROM charging_rates ORDER BY rate_per_kwh;")
                c_rates = cur.fetchall()

        return jsonify({
            "status": "success",
            "fees": fees_list,
            "payments": payments_list,
            "parking_rates": p_rates,
            "charging_rates": c_rates,
        }), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/payments/pay", methods=["POST"])
@login_required
def api_pay():
    user = session["user"]
    data = request.get_json(force=True)
    res_id = (data.get("reservation_id") or "").strip()
    raw_method = data.get("payment_method", "Ví điện tử")
    encrypted_method = "ENC_" + hash_text(raw_method)[:16].upper()
    pay_id = "PAY" + str(int(time.time()) % 100000)

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT r.total_amount, r.slot_id, v.user_id
                    FROM reservations r
                    JOIN vehicles v ON r.vehicle_id = v.vehicle_id
                    WHERE r.reservation_id = %s;
                """, (res_id,))
                res = cur.fetchone()
                if not res:
                    return jsonify({"status": "error", "message": "Không tìm thấy mã đặt chỗ này."}), 404

                if user["role"] == "customer" and res["user_id"] != user["user_id"]:
                    return jsonify({"status": "error", "message": "Bạn chỉ được thanh toán cho giao dịch của mình."}), 403

                cur.execute("""
                    INSERT INTO payments (payment_id, reservation_id, amount, payment_method, payment_status)
                    VALUES (%s, %s, %s, %s, 'Completed');
                """, (pay_id, res_id, res["total_amount"], encrypted_method))

                cur.execute("UPDATE reservations SET payment_status = 'Paid' WHERE reservation_id = %s;", (res_id,))
                cur.execute("UPDATE slots SET status = 'Available' WHERE slot_id = %s;", (res["slot_id"],))
            conn.commit()
        return jsonify({
            "status": "success",
            "message": f"Thanh toán thành công {int(res['total_amount']):,} VNĐ cho mã đặt chỗ {res_id}."
        }), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


@app.route("/api/rates/update", methods=["PUT"])
@admin_required
def api_update_rates():
    data = request.get_json(force=True)
    kind = data.get("kind")
    rate_id = data.get("rate_id")
    new_val = float(data.get("value") or 0)
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if kind == "parking":
                    cur.execute("UPDATE parking_rates SET price_per_hour = %s WHERE rate_id = %s;", (new_val, rate_id))
                else:
                    cur.execute("UPDATE charging_rates SET rate_per_kwh = %s WHERE rate_id = %s;", (new_val, rate_id))
            conn.commit()
        return jsonify({"status": "success", "message": f"Đã cập nhật đơn giá {rate_id} thành {int(new_val):,} VNĐ."}), 200
    except psycopg2.Error as e:
        return jsonify({"status": "error", "message": translate_pg_error(e)}), 400


# ==============================================================================
# GIAO DIỆN FRONTEND TIẾNG VIỆT 100% – THEME DARK EMERALD (#059669 - #10b981)
# ==============================================================================
FRONTEND_VI_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Smart City – Quản lý Trạm sạc & Bãi đỗ xe thông minh</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-top-bottom: #0b111e;
            --bg-middle: #1e293b;
            --card-bg: rgba(15, 23, 42, 0.82);
            --card-border: rgba(148, 163, 184, 0.16);
            --input-bg: #0f172a;
            --emerald-main: #10b981;
            --emerald-dark: #059669;
            --text-white: #f8fafc;
            --text-muted: #94a3b8;
        }
        * {
            box-sizing: border-box;
        }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(180deg, var(--bg-top-bottom) 0%, var(--bg-middle) 50%, var(--bg-top-bottom) 100%);
            background-attachment: fixed;
            color: var(--text-white);
            min-height: 100vh;
            margin: 0;
        }
        /* Trang Đăng nhập / Đăng ký ở giữa màn hình */
        .auth-wrapper {
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
        }
        .auth-card {
            width: 100%;
            max-width: 420px;
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 16px;
            padding: 36px 32px;
            box-shadow: 0 24px 48px rgba(0, 0, 0, 0.45);
            backdrop-filter: blur(12px);
        }
        .brand-pill {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 14px;
            border-radius: 999px;
            background: rgba(16, 185, 129, 0.12);
            border: 1px solid rgba(16, 185, 129, 0.35);
            color: var(--emerald-main);
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 16px;
        }
        .brand-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--emerald-main);
            display: inline-block;
        }
        .emerald-text {
            color: var(--emerald-main);
        }
        /* Sidebar cố định bên trái */
        .app-layout {
            display: flex;
            min-height: 100vh;
        }
        .sidebar {
            width: 275px;
            background: rgba(11, 17, 30, 0.95);
            border-right: 1px solid var(--card-border);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            position: fixed;
            top: 0;
            bottom: 0;
            left: 0;
            z-index: 100;
        }
        .sidebar-brand {
            padding: 22px 20px;
            border-bottom: 1px solid var(--card-border);
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .logo-hexagon {
            width: 36px;
            height: 36px;
            border-radius: 10px;
            background: linear-gradient(135deg, var(--emerald-dark), var(--emerald-main));
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            color: #ffffff;
            font-size: 18px;
        }
        .sidebar-menu {
            padding: 16px 12px;
            flex: 1;
            overflow-y: auto;
        }
        .nav-item-btn {
            width: 100%;
            text-align: left;
            background: transparent;
            border: 1px solid transparent;
            color: var(--text-muted);
            padding: 12px 14px;
            border-radius: 10px;
            margin-bottom: 6px;
            font-size: 14.5px;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 12px;
            transition: all 0.18s ease;
        }
        .nav-item-btn:hover {
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-white);
        }
        .nav-item-btn.active {
            background: var(--emerald-dark);
            border-color: var(--emerald-main);
            color: #ffffff;
            font-weight: 600;
            box-shadow: 0 4px 14px rgba(16, 185, 129, 0.28);
        }
        .sidebar-footer {
            padding: 18px 16px;
            background: rgba(7, 11, 20, 0.9);
            border-top: 1px solid var(--card-border);
        }
        .main-content {
            margin-left: 275px;
            flex: 1;
            padding: 32px 36px;
        }
        /* Card, Form, Input, Nút bấm và Bảng tối */
        .sc-card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 24px;
            margin-bottom: 24px;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.25);
        }
        .kpi-card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 20px;
            text-align: center;
        }
        .kpi-value {
            font-size: 28px;
            font-weight: 800;
            color: var(--text-white);
            margin-top: 6px;
        }
        .form-label {
            color: #cbd5e1;
            font-size: 13.5px;
            font-weight: 600;
            margin-bottom: 6px;
        }
        .form-control, .form-select {
            background-color: var(--input-bg) !important;
            border: 1px solid rgba(148, 163, 184, 0.25) !important;
            color: var(--text-white) !important;
            border-radius: 10px;
            padding: 10px 14px;
            font-size: 14px;
        }
        .form-control::placeholder {
            color: #64748b !important;
        }
        .form-control:focus, .form-select:focus {
            border-color: var(--emerald-main) !important;
            box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.2) !important;
        }
        .form-control[readonly] {
            background-color: rgba(15, 23, 42, 0.5) !important;
            color: var(--emerald-main) !important;
            font-weight: 600;
        }
        .btn-emerald {
            background: var(--emerald-dark);
            border: 1px solid var(--emerald-main);
            color: #ffffff;
            font-weight: 600;
            padding: 10px 20px;
            border-radius: 10px;
            transition: all 0.15s ease;
        }
        .btn-emerald:hover {
            background: var(--emerald-main);
            color: #06281e;
        }
        .btn-secondary-dark {
            background: #334155;
            border: 1px solid rgba(148, 163, 184, 0.25);
            color: var(--text-white);
            font-weight: 500;
            padding: 8px 14px;
            border-radius: 8px;
        }
        .btn-secondary-dark:hover {
            background: #475569;
            color: #ffffff;
        }
        /* Bảng dữ liệu nền tối */
        .table-dark-custom {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            color: var(--text-white);
            font-size: 14px;
        }
        .table-dark-custom thead th {
            background: rgba(15, 23, 42, 0.95);
            color: var(--text-muted);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 12px;
            letter-spacing: 0.4px;
            padding: 13px 14px;
            border-bottom: 1px solid var(--card-border);
        }
        .table-dark-custom tbody td {
            padding: 13px 14px;
            border-bottom: 1px solid rgba(148, 163, 184, 0.1);
            vertical-align: middle;
        }
        .table-dark-custom tbody tr:hover {
            background: rgba(16, 185, 129, 0.05);
        }
        /* Nhãn trạng thái dạng viên thuốc (pill) */
        .pill-emerald {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
            background: rgba(16, 185, 129, 0.15);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.35);
        }
        .pill-amber {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
            background: rgba(245, 158, 11, 0.15);
            color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.35);
        }
        .pill-red {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
            background: rgba(239, 68, 68, 0.15);
            color: #f87171;
            border: 1px solid rgba(239, 68, 68, 0.35);
        }
        .pill-slate {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
            background: rgba(148, 163, 184, 0.15);
            color: #cbd5e1;
            border: 1px solid rgba(148, 163, 184, 0.3);
        }
        .notice-box {
            padding: 14px 18px;
            border-radius: 10px;
            margin-bottom: 20px;
            font-weight: 500;
            font-size: 14px;
        }
        .notice-success {
            background: rgba(16, 185, 129, 0.15);
            border: 1px solid var(--emerald-main);
            color: #6ee7b7;
        }
        .notice-error {
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid #ef4444;
            color: #fca5a5;
        }
    </style>
</head>
<body>

<!-- ==================== 1. TRANG ĐĂNG NHẬP / ĐĂNG KÝ ==================== -->
<div id="authScreen" class="auth-wrapper">
    <div class="auth-card">
        <div class="text-center mb-4">
            <div class="brand-pill">
                <span class="brand-dot"></span>
                <span>Hệ thống Đô thị Thông minh</span>
            </div>
            <h3 class="fw-bold mb-1">Smart City <span class="emerald-text">EV</span></h3>
            <p class="small mb-0" style="color: var(--text-muted);">Quản lý Trạm sạc &amp; Bãi đỗ xe thông minh</p>
        </div>

        <div id="authAlert" class="notice-box d-none"></div>

        <!-- Form Đăng nhập (Ô email hoàn toàn để trống, không gợi ý tài khoản mẫu) -->
        <form id="loginForm" onsubmit="submitLogin(event)" autocomplete="off">
            <div class="mb-3">
                <label class="form-label">Email</label>
                <input type="email" id="loginEmail" class="form-control" placeholder="Nhập địa chỉ email" value="" required>
            </div>
            <div class="mb-4">
                <label class="form-label">Mật khẩu</label>
                <input type="password" id="loginPassword" class="form-control" placeholder="Nhập mật khẩu" value="" required>
            </div>
            <button type="submit" class="btn btn-emerald w-100 mb-3">Đăng nhập</button>
            <div class="text-center">
                <span class="small" style="color: var(--text-muted);">Chưa có tài khoản? </span>
                <a href="javascript:void(0)" onclick="toggleAuthMode('register')" class="emerald-text fw-semibold text-decoration-none">Đăng ký</a>
            </div>
        </form>

        <!-- Form Đăng ký -->
        <form id="registerForm" class="d-none" onsubmit="submitRegister(event)" autocomplete="off">
            <div class="mb-3">
                <label class="form-label">Họ và tên</label>
                <input type="text" id="regName" class="form-control" placeholder="Nhập họ và tên" required>
            </div>
            <div class="mb-3">
                <label class="form-label">Email</label>
                <input type="email" id="regEmail" class="form-control" placeholder="Nhập địa chỉ email" required>
            </div>
            <div class="mb-3">
                <label class="form-label">Số điện thoại</label>
                <input type="text" id="regPhone" class="form-control" placeholder="Nhập số điện thoại" required>
            </div>
            <div class="mb-4">
                <label class="form-label">Mật khẩu</label>
                <input type="password" id="regPassword" class="form-control" placeholder="Nhập mật khẩu" required>
            </div>
            <button type="submit" class="btn btn-emerald w-100 mb-3">Đăng ký</button>
            <div class="text-center">
                <a href="javascript:void(0)" onclick="toggleAuthMode('login')" class="emerald-text fw-semibold text-decoration-none">Quay lại Đăng nhập</a>
            </div>
        </form>
    </div>
</div>

<!-- ==================== 2. BỐ CỤC CHÍNH (SIDEBAR TRÁI + NỘI DUNG PHẢI) ==================== -->
<div id="appScreen" class="app-layout d-none">
    <aside class="sidebar">
        <div>
            <div class="sidebar-brand">
                <div class="logo-hexagon">⚡</div>
                <div>
                    <div class="fw-bold fs-6 text-white">Smart City <span class="emerald-text">EV</span></div>
                    <div style="font-size: 12px; color: var(--text-muted);">Trạm sạc &amp; Bãi đỗ xe</div>
                </div>
            </div>

            <div class="sidebar-menu">
                <button id="menu-overview" class="nav-item-btn" onclick="openPage('overview')">
                    <span>📊</span> <span>Tổng quan</span>
                </button>
                <button id="menu-users" class="nav-item-btn" onclick="openPage('users')">
                    <span>👥</span> <span>Quản lý người dùng</span>
                </button>
                <button id="menu-vehicles" class="nav-item-btn" onclick="openPage('vehicles')">
                    <span>🚗</span> <span>Quản lý phương tiện</span>
                </button>
                <button id="menu-stations" class="nav-item-btn" onclick="openPage('stations')">
                    <span>🔌</span> <span>Trạm sạc &amp; cổng sạc</span>
                </button>
                <button id="menu-slots" class="nav-item-btn" onclick="openPage('slots')">
                    <span>🅿️</span> <span>Bãi đỗ xe &amp; chỗ đỗ</span>
                </button>
                <button id="menu-reservations" class="nav-item-btn" onclick="openPage('reservations')">
                    <span>📅</span> <span>Đặt chỗ</span>
                </button>
                <button id="menu-sessions" class="nav-item-btn" onclick="openPage('sessions')">
                    <span>⚡</span> <span>Phiên sạc</span>
                </button>
                <button id="menu-billing" class="nav-item-btn" onclick="openPage('billing')">
                    <span>💳</span> <span>Thanh toán &amp; phí</span>
                </button>
            </div>
        </div>

        <!-- Phần TÀI KHOẢN ở dưới cùng: chỉ hiển thị Vai trò, Họ tên và nút Đăng xuất (KHÔNG hiển thị email) -->
        <div class="sidebar-footer">
            <div class="pill-emerald mb-2" id="userRoleBadge">Quản trị hệ thống</div>
            <div class="fw-bold text-white text-truncate mb-3" id="userNameLabel">Người dùng</div>
            <button onclick="handleLogout()" class="btn btn-secondary-dark w-100">Đăng xuất</button>
        </div>
    </aside>

    <main class="main-content">
        <div id="globalNotice" class="notice-box d-none"></div>

        <!-- 1. TỔNG QUAN -->
        <section id="page-overview" class="page-section d-none">
            <div class="d-flex justify-content-between align-items-center mb-4">
                <div>
                    <h2 class="fw-bold mb-1">Tổng quan <span class="emerald-text">Hệ thống</span></h2>
                    <p class="mb-0" style="color: var(--text-muted);">Theo dõi hoạt động trạm sạc, chỗ đỗ trống và các lượt đặt chỗ đang diễn ra</p>
                </div>
                <span id="overviewSpeedBadge" class="pill-emerald">Thời gian truy vấn: 1.5 ms</span>
            </div>

            <div class="row g-3 mb-4">
                <div class="col-md-3">
                    <div class="kpi-card">
                        <div class="small" style="color: var(--text-muted);">Chỗ đỗ còn trống</div>
                        <div class="kpi-value emerald-text" id="kpiAvailSlots">0</div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="kpi-card">
                        <div class="small" style="color: var(--text-muted);">Trụ sạc trực tuyến</div>
                        <div class="kpi-value" id="kpiOnlineStations">0</div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="kpi-card">
                        <div class="small" style="color: var(--text-muted);">Đặt chỗ đang hiệu lực</div>
                        <div class="kpi-value" id="kpiActiveRes">0</div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="kpi-card">
                        <div class="small" style="color: var(--text-muted);">Tổng doanh thu đã thu</div>
                        <div class="kpi-value emerald-text" id="kpiTotalRev">0 VNĐ</div>
                    </div>
                </div>
            </div>

            <div class="sc-card">
                <h5 class="fw-bold mb-1">Danh sách xe xăng đỗ tại chỗ sạc điện</h5>
                <p class="small mb-3" style="color: var(--text-muted);">Tự động phát hiện và chuyển mức phí phạt vi phạm sang bộ phận thanh toán</p>
                <div class="table-responsive">
                    <table class="table-dark-custom">
                        <thead>
                            <tr>
                                <th>Mã đặt chỗ</th>
                                <th>Chủ phương tiện</th>
                                <th>Biển số xe</th>
                                <th>Mã chỗ sạc</th>
                                <th>Mức phí vi phạm</th>
                                <th>Tổng chi phí</th>
                            </tr>
                        </thead>
                        <tbody id="overviewViolationBody"></tbody>
                    </table>
                </div>
            </div>

            <div class="sc-card">
                <h5 class="fw-bold mb-1">Các lượt đặt chỗ đang hiệu lực</h5>
                <p class="small mb-3" style="color: var(--text-muted);">Danh sách các lượt đặt chỗ chờ thanh toán và chưa hết thời gian đỗ</p>
                <div class="table-responsive">
                    <table class="table-dark-custom">
                        <thead>
                            <tr>
                                <th>Mã đặt chỗ</th>
                                <th>Khách hàng</th>
                                <th>Biển số xe</th>
                                <th>Khu vực &amp; Chỗ đỗ</th>
                                <th>Thời gian bắt đầu</th>
                                <th>Thời gian kết thúc</th>
                                <th>Tổng chi phí</th>
                            </tr>
                        </thead>
                        <tbody id="overviewActiveViewBody"></tbody>
                    </table>
                </div>
            </div>
        </section>

        <!-- 2. QUẢN LÝ NGƯỜI DÙNG -->
        <section id="page-users" class="page-section d-none">
            <h2 class="fw-bold mb-1">Quản lý <span class="emerald-text">Người dùng</span></h2>
            <p class="mb-4" style="color: var(--text-muted);">Thêm mới hoặc xóa tài khoản khách hàng trong hệ thống</p>

            <div class="sc-card">
                <h5 class="fw-bold mb-3">Thêm người dùng mới</h5>
                <form class="row g-3" onsubmit="handleCreateUser(event)" autocomplete="off">
                    <div class="col-md-2">
                        <label class="form-label">Mã người dùng</label>
                        <input type="text" id="u_id" class="form-control" placeholder="Nhập mã" required>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Họ và tên</label>
                        <input type="text" id="u_name" class="form-control" placeholder="Nhập họ tên" required>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Email (@gmail.com)</label>
                        <input type="email" id="u_email" class="form-control" placeholder="Nhập email" required>
                    </div>
                    <div class="col-md-2">
                        <label class="form-label">Số điện thoại</label>
                        <input type="text" id="u_phone" class="form-control" placeholder="Nhập số điện thoại" required>
                    </div>
                    <div class="col-md-2 d-flex align-items-end">
                        <button type="submit" class="btn btn-emerald w-100">Thêm mới</button>
                    </div>
                </form>
            </div>

            <div class="sc-card">
                <h5 class="fw-bold mb-3">Danh sách người dùng</h5>
                <div class="table-responsive">
                    <table class="table-dark-custom">
                        <thead>
                            <tr>
                                <th>Mã người dùng</th>
                                <th>Họ và tên</th>
                                <th>Email</th>
                                <th>Số điện thoại</th>
                                <th>Số lượng xe</th>
                                <th>Thao tác</th>
                            </tr>
                        </thead>
                        <tbody id="usersTableBody"></tbody>
                    </table>
                </div>
            </div>
        </section>

        <!-- 3. QUẢN LÝ PHƯƠNG TIỆN -->
        <section id="page-vehicles" class="page-section d-none">
            <h2 class="fw-bold mb-1">Quản lý <span class="emerald-text">Phương tiện</span></h2>
            <p class="mb-4" style="color: var(--text-muted);">Đăng ký và quản lý hồ sơ xe điện và xe xăng</p>

            <div class="sc-card">
                <h5 class="fw-bold mb-3">Đăng ký phương tiện mới</h5>
                <form class="row g-3" onsubmit="handleCreateVehicle(event)" autocomplete="off">
                    <div class="col-md-3">
                        <label class="form-label">Mã phương tiện</label>
                        <input type="text" id="v_id" class="form-control" placeholder="Nhập mã phương tiện" required>
                    </div>
                    <div class="col-md-4">
                        <label class="form-label">Biển số xe</label>
                        <input type="text" id="v_plate" class="form-control" placeholder="Nhập biển số xe" required>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Loại phương tiện</label>
                        <select id="v_type" class="form-select">
                            <option value="EV">Xe điện</option>
                            <option value="ICE">Xe xăng</option>
                        </select>
                    </div>
                    <div class="col-md-2 d-flex align-items-end">
                        <button type="submit" class="btn btn-emerald w-100">Đăng ký xe</button>
                    </div>
                </form>
            </div>

            <div class="sc-card">
                <h5 class="fw-bold mb-3">Danh sách phương tiện đã đăng ký</h5>
                <div class="table-responsive">
                    <table class="table-dark-custom">
                        <thead>
                            <tr>
                                <th>Mã phương tiện</th>
                                <th>Chủ sở hữu</th>
                                <th>Biển số xe</th>
                                <th>Loại phương tiện</th>
                            </tr>
                        </thead>
                        <tbody id="vehiclesTableBody"></tbody>
                    </table>
                </div>
            </div>
        </section>

        <!-- 4. TRẠM SẠC & CỔNG SẠC -->
        <section id="page-stations" class="page-section d-none">
            <h2 class="fw-bold mb-1">Trạm sạc &amp; <span class="emerald-text">Cổng sạc</span></h2>
            <p class="mb-4" style="color: var(--text-muted);">Thông tin các trụ sạc, chuẩn cổng kết nối, công suất và mức phí quy định</p>

            <div class="sc-card">
                <div class="table-responsive">
                    <table class="table-dark-custom">
                        <thead>
                            <tr>
                                <th>Mã trụ sạc</th>
                                <th>Mã chỗ đỗ</th>
                                <th>Khu vực bãi đỗ</th>
                                <th>Loại bộ sạc</th>
                                <th>Chuẩn cổng sạc</th>
                                <th>Công suất tối đa</th>
                                <th>Phí chiếm chỗ / giờ</th>
                                <th>Phí vi phạm</th>
                                <th>Trạng thái</th>
                            </tr>
                        </thead>
                        <tbody id="stationsTableBody"></tbody>
                    </table>
                </div>
            </div>
        </section>

        <!-- 5. BÃI ĐỖ XE & CHỖ ĐỖ -->
        <section id="page-slots" class="page-section d-none">
            <div class="d-flex justify-content-between align-items-center mb-4">
                <div>
                    <h2 class="fw-bold mb-1">Bãi đỗ xe &amp; <span class="emerald-text">Chỗ đỗ</span></h2>
                    <p class="mb-0" style="color: var(--text-muted);">Tra cứu nhanh chỗ đỗ và cập nhật trạng thái vận hành</p>
                </div>
                <span id="slotSearchTimeBadge" class="pill-emerald">Thời gian tìm kiếm: 1.2 ms</span>
            </div>

            <div class="sc-card">
                <div class="row g-3 align-items-end">
                    <div class="col-md-4">
                        <label class="form-label">Khu vực bãi đỗ</label>
                        <select id="filterSlotLocation" class="form-select" onchange="loadSlots()">
                            <option value="">Tất cả khu vực</option>
                            <option value="LOC001">Trạm trung tâm Sài Gòn Centre</option>
                            <option value="LOC002">Khu công nghệ cao Thủ Đức</option>
                            <option value="LOC003">Bãi đỗ thông minh Landmark 81</option>
                        </select>
                    </div>
                    <div class="col-md-4">
                        <label class="form-label">Loại chỗ đỗ</label>
                        <select id="filterSlotType" class="form-select" onchange="loadSlots()">
                            <option value="">Tất cả loại chỗ đỗ</option>
                            <option value="Charging">Chỗ đỗ có sạc điện</option>
                            <option value="Standard">Chỗ đỗ tiêu chuẩn</option>
                        </select>
                    </div>
                    <div class="col-md-4">
                        <div class="form-check mb-2">
                            <input class="form-check-input" type="checkbox" id="filterOnlyAvailable" onchange="loadSlots()">
                            <label class="form-check-label text-white fw-semibold" for="filterOnlyAvailable">
                                Chỉ hiển thị chỗ đỗ còn trống
                            </label>
                        </div>
                    </div>
                </div>
            </div>

            <div class="sc-card">
                <div class="table-responsive">
                    <table class="table-dark-custom">
                        <thead>
                            <tr>
                                <th>Mã chỗ đỗ</th>
                                <th>Khu vực bãi đỗ</th>
                                <th>Tầng</th>
                                <th>Loại chỗ đỗ</th>
                                <th>Thiết bị sạc</th>
                                <th>Trạng thái</th>
                                <th>Cập nhật trạng thái</th>
                            </tr>
                        </thead>
                        <tbody id="slotsTableBody"></tbody>
                    </table>
                </div>
            </div>
        </section>

        <!-- 6. ĐẶT CHỖ -->
        <section id="page-reservations" class="page-section d-none">
            <div class="d-flex justify-content-between align-items-center mb-4">
                <div>
                    <h2 class="fw-bold mb-1">Đặt chỗ <span class="emerald-text">Đỗ xe &amp; Sạc điện</span></h2>
                    <p class="mb-0" style="color: var(--text-muted);">Tìm kiếm chỗ đỗ còn trống và đặt trước theo khoảng thời gian</p>
                </div>
                <span id="resSlotSpeedBadge" class="pill-emerald">Thời gian tìm kiếm: 1.4 ms</span>
            </div>

            <div class="sc-card">
                <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2">
                    <h5 class="fw-bold mb-0">Tra cứu chỗ đỗ / trạm sạc còn trống</h5>
                    <div class="d-flex gap-2">
                        <select id="quickLocFilter" class="form-select" style="width:auto;" onchange="loadAvailableSlotsForBooking()">
                            <option value="">Tất cả khu vực</option>
                            <option value="LOC001">Trạm trung tâm Sài Gòn Centre</option>
                            <option value="LOC002">Khu công nghệ cao Thủ Đức</option>
                        </select>
                        <select id="quickTypeFilter" class="form-select" style="width:auto;" onchange="loadAvailableSlotsForBooking()">
                            <option value="">Tất cả loại chỗ</option>
                            <option value="Charging">Chỗ đỗ có sạc điện</option>
                            <option value="Standard">Chỗ đỗ tiêu chuẩn</option>
                        </select>
                    </div>
                </div>
                <div class="table-responsive">
                    <table class="table-dark-custom">
                        <thead>
                            <tr>
                                <th>Mã chỗ đỗ</th>
                                <th>Khu vực bãi đỗ</th>
                                <th>Tầng</th>
                                <th>Loại chỗ đỗ</th>
                                <th>Thông số trụ sạc</th>
                                <th>Trạng thái</th>
                                <th>Thao tác</th>
                            </tr>
                        </thead>
                        <tbody id="bookingAvailableSlotsBody"></tbody>
                    </table>
                </div>
            </div>

            <div class="sc-card">
                <h5 class="fw-bold mb-3">Tạo lượt đặt chỗ mới</h5>
                <form class="row g-3" onsubmit="handleCreateReservation(event)" autocomplete="off">
                    <div class="col-md-3">
                        <label class="form-label">Mã đặt chỗ</label>
                        <input type="text" id="r_id" class="form-control" placeholder="Nhập mã đặt chỗ" required>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Phương tiện</label>
                        <select id="r_vehicle" class="form-select"></select>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Chỗ đỗ</label>
                        <select id="r_slot" class="form-select">
                            <option value="SLT001">SLT001 - Sài Gòn Centre (Chỗ sạc)</option>
                            <option value="SLT002">SLT002 - Sài Gòn Centre (Chỗ sạc)</option>
                            <option value="SLT003">SLT003 - Sài Gòn Centre (Chỗ thường)</option>
                            <option value="SLT004">SLT004 - Thủ Đức (Chỗ sạc)</option>
                            <option value="SLT005">SLT005 - Thủ Đức (Chỗ sạc)</option>
                            <option value="SLT006">SLT006 - Thủ Đức (Chỗ thường)</option>
                        </select>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Khung giá</label>
                        <select id="r_rate" class="form-select">
                            <option value="PR_NORMAL">Giờ bình thường (25.000 VNĐ/giờ)</option>
                            <option value="PR_PEAK">Giờ cao điểm (45.000 VNĐ/giờ)</option>
                            <option value="PR_OFFPEAK">Giờ thấp điểm (15.000 VNĐ/giờ)</option>
                        </select>
                    </div>
                    <div class="col-md-4">
                        <label class="form-label">Thời gian bắt đầu</label>
                        <input type="datetime-local" id="r_start" class="form-control" required>
                    </div>
                    <div class="col-md-4">
                        <label class="form-label">Thời gian kết thúc</label>
                        <input type="datetime-local" id="r_end" class="form-control" required>
                    </div>
                    <div class="col-md-4 d-flex align-items-end">
                        <button type="submit" class="btn btn-emerald w-100">Đặt chỗ</button>
                    </div>
                </form>
            </div>

            <div class="sc-card">
                <h5 class="fw-bold mb-3">Danh sách đặt chỗ</h5>
                <div class="table-responsive">
                    <table class="table-dark-custom">
                        <thead>
                            <tr>
                                <th>Mã đặt chỗ</th>
                                <th>Khách hàng</th>
                                <th>Biển số &amp; Loại xe</th>
                                <th>Chỗ đỗ</th>
                                <th>Thời gian bắt đầu</th>
                                <th>Thời gian kết thúc</th>
                                <th>Tổng chi phí</th>
                                <th>Trạng thái thanh toán</th>
                            </tr>
                        </thead>
                        <tbody id="reservationsTableBody"></tbody>
                    </table>
                </div>
            </div>
        </section>

        <!-- 7. PHIÊN SẠC (SỐ ĐIỆN KWH DO HỆ THỐNG TỰ ĐỘNG TÍNH VÀ CHỈ HIỂN THỊ CHỈ ĐỌC) -->
        <section id="page-sessions" class="page-section d-none">
            <h2 class="fw-bold mb-1">Phiên <span class="emerald-text">Sạc điện</span></h2>
            <p class="mb-4" style="color: var(--text-muted);">Hệ thống tự động đo đếm điện năng tiêu thụ (kWh) dựa trên thời gian sạc và công suất trụ sạc</p>

            <div class="sc-card">
                <h5 class="fw-bold mb-3">Ghi nhận phiên sạc</h5>
                <form class="row g-3" onsubmit="handleCreateSession(event)" autocomplete="off">
                    <div class="col-md-3">
                        <label class="form-label">Mã đặt chỗ liên kết</label>
                        <input type="text" id="s_res_id" class="form-control" placeholder="Nhập mã đặt chỗ">
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Phương tiện sạc</label>
                        <select id="s_vehicle" class="form-select"></select>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Trụ sạc</label>
                        <select id="s_station" class="form-select" onchange="previewAutoKwh()">
                            <option value="STN001" data-kw="60">STN001 (60 kW - SLT001)</option>
                            <option value="STN002" data-kw="120">STN002 (120 kW - SLT002)</option>
                            <option value="STN003" data-kw="22">STN003 (22 kW - SLT004)</option>
                            <option value="STN004" data-kw="60">STN004 (60 kW - SLT005)</option>
                        </select>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Khung giá điện</label>
                        <select id="s_rate" class="form-select">
                            <option value="CR_NORMAL">Giờ bình thường (3.500 VNĐ/kWh)</option>
                            <option value="CR_PEAK">Giờ cao điểm (4.800 VNĐ/kWh)</option>
                            <option value="CR_OFFPEAK">Giờ thấp điểm (2.800 VNĐ/kWh)</option>
                        </select>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Thời gian bắt đầu sạc</label>
                        <input type="datetime-local" id="s_start" class="form-control" onchange="previewAutoKwh()" required>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Thời gian kết thúc sạc</label>
                        <input type="datetime-local" id="s_end" class="form-control" onchange="previewAutoKwh()" required>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Số điện tiêu thụ (Hệ thống tự tính)</label>
                        <input type="text" id="s_kwh_readonly" class="form-control" value="Tự động ghi nhận (kWh)" readonly tabindex="-1">
                    </div>
                    <div class="col-md-3 d-flex align-items-end">
                        <button type="submit" class="btn btn-emerald w-100">Hoàn tất phiên sạc</button>
                    </div>
                </form>
            </div>

            <div class="sc-card">
                <h5 class="fw-bold mb-3">Danh sách các phiên sạc đã ghi nhận</h5>
                <div class="table-responsive">
                    <table class="table-dark-custom">
                        <thead>
                            <tr>
                                <th>Mã phiên sạc</th>
                                <th>Biển số xe</th>
                                <th>Mã trụ sạc</th>
                                <th>Bắt đầu sạc</th>
                                <th>Kết thúc sạc</th>
                                <th>Điện năng tiêu thụ (Chỉ đọc)</th>
                                <th>Đơn giá / kWh</th>
                                <th>Thành tiền sạc</th>
                            </tr>
                        </thead>
                        <tbody id="sessionsTableBody"></tbody>
                    </table>
                </div>
            </div>
        </section>

        <!-- 8. THANH TOÁN & PHÍ -->
        <section id="page-billing" class="page-section d-none">
            <h2 class="fw-bold mb-1">Thanh toán &amp; <span class="emerald-text">Biểu phí</span></h2>
            <p class="mb-4" style="color: var(--text-muted);">Thanh toán giao dịch đã hoàn tất và tra cứu chi tiết các khoản phí</p>

            <div class="row g-4">
                <div class="col-lg-6">
                    <div class="sc-card h-100 mb-0">
                        <h5 class="fw-bold mb-3">Thanh toán hóa đơn giao dịch</h5>
                        <form onsubmit="handlePayInvoice(event)" class="row g-3" autocomplete="off">
                            <div class="col-md-6">
                                <label class="form-label">Mã đặt chỗ</label>
                                <input type="text" id="pay_res_id" class="form-control" placeholder="Nhập mã đặt chỗ" required>
                            </div>
                            <div class="col-md-6">
                                <label class="form-label">Phương thức thanh toán</label>
                                <select id="pay_method" class="form-select">
                                    <option value="Ví điện tử">Ví điện tử</option>
                                    <option value="Thẻ tín dụng">Thẻ tín dụng</option>
                                    <option value="Mã QR">Mã QR</option>
                                    <option value="Chuyển khoản ngân hàng">Chuyển khoản ngân hàng</option>
                                </select>
                            </div>
                            <div class="col-12">
                                <button type="submit" class="btn btn-emerald w-100">Thanh toán</button>
                            </div>
                        </form>
                    </div>
                </div>

                <!-- Cấu hình giá dành riêng cho Quản trị hệ thống -->
                <div class="col-lg-6" id="adminRateConfigCard">
                    <div class="sc-card h-100 mb-0">
                        <h5 class="fw-bold mb-3">Cấu hình biểu giá động</h5>
                        <form onsubmit="handleUpdateRate(event)" class="row g-2 mb-3" autocomplete="off">
                            <div class="col-md-4">
                                <select id="cfg_kind" class="form-select">
                                    <option value="parking">Giá đỗ xe (/giờ)</option>
                                    <option value="charging">Giá sạc điện (/kWh)</option>
                                </select>
                            </div>
                            <div class="col-md-4">
                                <input type="text" id="cfg_rate_id" class="form-control" placeholder="Mã khung giá" required>
                            </div>
                            <div class="col-md-4 d-flex gap-2">
                                <input type="number" id="cfg_val" class="form-control" placeholder="Đơn giá" required>
                                <button type="submit" class="btn btn-emerald">Lưu</button>
                            </div>
                        </form>
                        <div class="table-responsive">
                            <table class="table-dark-custom">
                                <thead>
                                    <tr>
                                        <th>Mã khung giá</th>
                                        <th>Khung giờ</th>
                                        <th>Đơn giá</th>
                                    </tr>
                                </thead>
                                <tbody id="ratesConfigBody"></tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>

            <div class="sc-card mt-4">
                <h5 class="fw-bold mb-3">Danh sách phí vi phạm &amp; phí chiếm chỗ</h5>
                <div class="table-responsive">
                    <table class="table-dark-custom">
                        <thead>
                            <tr>
                                <th>Mã phí</th>
                                <th>Mã đặt chỗ</th>
                                <th>Khách hàng &amp; Biển số</th>
                                <th>Loại phí</th>
                                <th>Thời gian vượt</th>
                                <th>Số tiền</th>
                            </tr>
                        </thead>
                        <tbody id="feesTableBody"></tbody>
                    </table>
                </div>
            </div>

            <div class="sc-card">
                <h5 class="fw-bold mb-3">Lịch sử thanh toán</h5>
                <div class="table-responsive">
                    <table class="table-dark-custom">
                        <thead>
                            <tr>
                                <th>Mã thanh toán</th>
                                <th>Mã đặt chỗ</th>
                                <th>Khách hàng</th>
                                <th>Thời điểm thanh toán</th>
                                <th>Số tiền</th>
                                <th>Mã bảo mật thanh toán</th>
                                <th>Trạng thái</th>
                            </tr>
                        </thead>
                        <tbody id="paymentsTableBody"></tbody>
                    </table>
                </div>
            </div>
        </section>
    </main>
</div>

<script>
let currentUser = null;

function formatVND(val) {
    return Number(val || 0).toLocaleString('vi-VN') + ' VNĐ';
}

function formatDateTimeVI(val) {
    if (!val) return '';
    const d = new Date(val);
    if (isNaN(d.getTime())) return String(val).replace('GMT', '').trim();
    return d.toLocaleString('vi-VN', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false
    });
}

function translateStatus(st) {
    const map = {
        'Available': '<span class="pill-emerald">Còn trống</span>',
        'Occupied': '<span class="pill-red">Đang có xe</span>',
        'Reserved': '<span class="pill-amber">Đã đặt trước</span>',
        'Maintenance': '<span class="pill-slate">Đang bảo trì</span>',
        'Online': '<span class="pill-emerald">Trực tuyến</span>',
        'Busy': '<span class="pill-amber">Đang sạc</span>',
        'Offline': '<span class="pill-slate">Ngoại tuyến</span>',
        'Faulted': '<span class="pill-red">Báo lỗi</span>',
        'Pending': '<span class="pill-amber">Chờ thanh toán</span>',
        'Paid': '<span class="pill-emerald">Đã thanh toán</span>',
        'Completed': '<span class="pill-emerald">Đã thanh toán</span>',
        'Failed': '<span class="pill-red">Thất bại</span>',
        'Refunded': '<span class="pill-slate">Đã hoàn tiền</span>'
    };
    return map[st] || `<span class="pill-slate">${st}</span>`;
}

function translateRole(role) {
    return (role === 'system_admin') ? 'Quản trị hệ thống' : 'Khách hàng';
}

function checkGmailClient(emailStr) {
    const e = (emailStr || '').trim().toLowerCase();
    return e.length > 10 && e.endsWith('@gmail.com') && !e.includes(' ');
}

function showNotice(msg, isError = false) {
    const el = document.getElementById('globalNotice');
    el.className = 'notice-box ' + (isError ? 'notice-error' : 'notice-success');
    el.textContent = msg;
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function showAuthError(msg, isError = true) {
    const al = document.getElementById('authAlert');
    al.className = 'notice-box ' + (isError ? 'notice-error' : 'notice-success');
    al.textContent = msg;
}

function toggleAuthMode(mode) {
    document.getElementById('authAlert').classList.add('d-none');
    if (mode === 'register') {
        document.getElementById('loginForm').classList.add('d-none');
        document.getElementById('registerForm').classList.remove('d-none');
    } else {
        document.getElementById('registerForm').classList.add('d-none');
        document.getElementById('loginForm').classList.remove('d-none');
    }
}

async function submitLogin(e) {
    e.preventDefault();
    const email = document.getElementById('loginEmail').value.trim();
    const password = document.getElementById('loginPassword').value;
    if (!checkGmailClient(email)) {
        showAuthError('Email phải có đuôi @gmail.com');
        return;
    }
    const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password })
    });
    const data = await resp.json();
    if (!resp.ok) {
        showAuthError(data.message);
        return;
    }
    currentUser = data.user;
    enterApp();
}

async function submitRegister(e) {
    e.preventDefault();
    const email = document.getElementById('regEmail').value.trim();
    if (!checkGmailClient(email)) {
        showAuthError('Email phải có đuôi @gmail.com');
        return;
    }
    const payload = {
        name: document.getElementById('regName').value.trim(),
        email: email,
        phone_number: document.getElementById('regPhone').value.trim(),
        password: document.getElementById('regPassword').value
    };
    const resp = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    const data = await resp.json();
    if (!resp.ok) {
        showAuthError(data.message, true);
    } else {
        toggleAuthMode('login');
        showAuthError(data.message, false);
    }
}

async function handleLogout() {
    await fetch('/api/auth/logout', { method: 'POST' });
    currentUser = null;
    document.getElementById('loginEmail').value = '';
    document.getElementById('loginPassword').value = '';
    document.getElementById('appScreen').classList.add('d-none');
    document.getElementById('authScreen').classList.remove('d-none');
}

function applyRBACMenu() {
    const isAdmin = (currentUser.role === 'system_admin');
    document.getElementById('userRoleBadge').textContent = translateRole(currentUser.role);
    document.getElementById('userNameLabel').textContent = currentUser.name;

    document.getElementById('menu-overview').style.display = isAdmin ? 'flex' : 'none';
    document.getElementById('menu-users').style.display = isAdmin ? 'flex' : 'none';
    document.getElementById('menu-stations').style.display = isAdmin ? 'flex' : 'none';
    document.getElementById('menu-slots').style.display = isAdmin ? 'flex' : 'none';

    document.getElementById('menu-vehicles').style.display = 'flex';
    document.getElementById('menu-reservations').style.display = 'flex';
    document.getElementById('menu-sessions').style.display = 'flex';
    document.getElementById('menu-billing').style.display = 'flex';

    document.getElementById('adminRateConfigCard').style.display = isAdmin ? 'block' : 'none';
}

function enterApp() {
    document.getElementById('authScreen').classList.add('d-none');
    document.getElementById('appScreen').classList.remove('d-none');
    applyRBACMenu();
    if (currentUser.role === 'system_admin') {
        openPage('overview');
    } else {
        openPage('vehicles');
    }
}

function openPage(pageKey) {
    document.getElementById('globalNotice').classList.add('d-none');
    document.querySelectorAll('.page-section').forEach(s => s.classList.add('d-none'));
    document.querySelectorAll('.nav-item-btn').forEach(b => b.classList.remove('active'));

    document.getElementById('page-' + pageKey).classList.remove('d-none');
    const btn = document.getElementById('menu-' + pageKey);
    if (btn) btn.classList.add('active');

    if (pageKey === 'overview') loadOverview();
    if (pageKey === 'users') loadUsers();
    if (pageKey === 'vehicles') loadVehicles();
    if (pageKey === 'stations') loadStations();
    if (pageKey === 'slots') loadSlots();
    if (pageKey === 'reservations') {
        loadVehiclesDropdown();
        loadAvailableSlotsForBooking();
        loadReservations();
    }
    if (pageKey === 'sessions') {
        loadVehiclesDropdown();
        loadSessions();
    }
    if (pageKey === 'billing') loadBilling();
}

function previewAutoKwh() {
    const sStart = document.getElementById('s_start').value;
    const sEnd = document.getElementById('s_end').value;
    const sel = document.getElementById('s_station');
    const kw = Number(sel.options[sel.selectedIndex]?.getAttribute('data-kw') || 22);
    if (!sStart || !sEnd) {
        document.getElementById('s_kwh_readonly').value = 'Tự động ghi nhận (kWh)';
        return;
    }
    const diffHours = (new Date(sEnd) - new Date(sStart)) / 3600000;
    if (diffHours > 0) {
        const est = Math.max(0.5, diffHours * kw * 0.85).toFixed(2);
        document.getElementById('s_kwh_readonly').value = est + ' kWh (Hệ thống tự tính)';
    } else {
        document.getElementById('s_kwh_readonly').value = 'Thời gian chưa hợp lệ';
    }
}

async function loadVehiclesDropdown() {
    const resp = await fetch('/api/vehicles');
    const data = await resp.json();
    if (!resp.ok || !data.data) return;
    const opts = data.data.map(v =>
        `<option value="${v.vehicle_id}">${v.vehicle_id} - ${v.plate_number} (${v.vehicle_type === 'EV' ? 'Xe điện' : 'Xe xăng'})</option>`
    ).join('');
    document.getElementById('r_vehicle').innerHTML = opts;
    document.getElementById('s_vehicle').innerHTML = opts;
}

async function loadAvailableSlotsForBooking() {
    const loc = document.getElementById('quickLocFilter').value;
    const stype = document.getElementById('quickTypeFilter').value;
    const resp = await fetch(`/api/slots?location_id=${encodeURIComponent(loc)}&slot_type=${encodeURIComponent(stype)}&only_available=true`);
    const data = await resp.json();
    if (!resp.ok) return;
    document.getElementById('resSlotSpeedBadge').textContent = 'Thời gian tìm kiếm: ' + data.query_time_ms + ' ms';
    document.getElementById('bookingAvailableSlotsBody').innerHTML = data.data.map(s => `
        <tr>
            <td class="fw-bold emerald-text">${s.slot_id}</td>
            <td>${s.location_name}</td>
            <td>Tầng ${s.floor}</td>
            <td>${s.slot_type === 'Charging' ? '<span class="pill-emerald">Chỗ sạc điện</span>' : '<span class="pill-slate">Chỗ đỗ thường</span>'}</td>
            <td>${s.charger_type ? `${s.charger_type} (${s.connector_type} - ${s.max_power_kw} kW)` : 'Không lắp trụ sạc'}</td>
            <td>${translateStatus(s.status)}</td>
            <td>
                <button type="button" onclick="selectSlotForBooking('${s.slot_id}')" class="btn btn-secondary-dark btn-sm">Chọn chỗ này</button>
            </td>
        </tr>
    `).join('') || '<tr><td colspan="7" class="text-center" style="color:var(--text-muted);">Không có chỗ đỗ trống phù hợp.</td></tr>';
}

function selectSlotForBooking(slotId) {
    document.getElementById('r_slot').value = slotId;
    showNotice('Đã chọn chỗ đỗ ' + slotId + ' vào biểu mẫu đặt chỗ.');
}

async function loadOverview() {
    const resp = await fetch('/api/overview');
    const data = await resp.json();
    if (!resp.ok) return showNotice(data.message, true);

    document.getElementById('overviewSpeedBadge').textContent = 'Thời gian truy vấn: ' + data.query_time_ms + ' ms';
    document.getElementById('kpiAvailSlots').textContent = data.kpis.avail_slots;
    document.getElementById('kpiOnlineStations').textContent = data.kpis.online_stations;
    document.getElementById('kpiActiveRes').textContent = data.kpis.active_res;
    document.getElementById('kpiTotalRev').textContent = formatVND(data.kpis.total_rev);

    document.getElementById('overviewViolationBody').innerHTML = (data.violations || []).map(v => `
        <tr>
            <td class="fw-bold">${v.reservation_id}</td>
            <td>${v.owner_name}</td>
            <td class="fw-bold text-danger">${v.plate_number}</td>
            <td class="fw-bold">${v.slot_id}</td>
            <td class="text-danger fw-bold">${formatVND(v.violation_fee_rate)}</td>
            <td class="fw-bold">${formatVND(v.total_amount)}</td>
        </tr>
    `).join('') || '<tr><td colspan="6" class="text-center" style="color:var(--text-muted);">Không có trường hợp vi phạm nào.</td></tr>';

    document.getElementById('overviewActiveViewBody').innerHTML = (data.active_reservations || []).map(r => `
        <tr>
            <td class="fw-bold">${r.reservation_id}</td>
            <td>${r.customer_name}</td>
            <td class="fw-bold">${r.plate_number} (${r.vehicle_type === 'EV' ? 'Xe điện' : 'Xe xăng'})</td>
            <td>${r.location_name} - <b>${r.slot_id}</b></td>
            <td>${formatDateTimeVI(r.start_time)}</td>
            <td>${formatDateTimeVI(r.end_time)}</td>
            <td class="fw-bold emerald-text">${formatVND(r.total_amount)}</td>
        </tr>
    `).join('') || '<tr><td colspan="7" class="text-center" style="color:var(--text-muted);">Không có lượt đặt chỗ nào đang chờ.</td></tr>';
}

async function loadUsers() {
    const resp = await fetch('/api/users');
    const data = await resp.json();
    if (!resp.ok) return showNotice(data.message, true);
    document.getElementById('usersTableBody').innerHTML = data.data.map(u => `
        <tr>
            <td class="fw-bold">${u.user_id}</td>
            <td>${u.name}</td>
            <td>${u.email}</td>
            <td>${u.phone_number}</td>
            <td>${u.vehicle_count}</td>
            <td>
                <button onclick="handleDeleteUser('${u.user_id}')" class="btn btn-secondary-dark btn-sm">Xóa</button>
            </td>
        </tr>
    `).join('');
}

async function handleCreateUser(e) {
    e.preventDefault();
    const email = document.getElementById('u_email').value.trim();
    if (!checkGmailClient(email)) {
        showNotice('Email phải có đuôi @gmail.com', true);
        return;
    }
    const resp = await fetch('/api/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            user_id: document.getElementById('u_id').value.trim(),
            name: document.getElementById('u_name').value.trim(),
            email: email,
            phone_number: document.getElementById('u_phone').value.trim()
        })
    });
    const data = await resp.json();
    showNotice(data.message, !resp.ok);
    if (resp.ok) loadUsers();
}

async function handleDeleteUser(uid) {
    const resp = await fetch('/api/users/' + uid, { method: 'DELETE' });
    const data = await resp.json();
    showNotice(data.message, !resp.ok);
    if (resp.ok) loadUsers();
}

async function loadVehicles() {
    const resp = await fetch('/api/vehicles');
    const data = await resp.json();
    if (!resp.ok) return showNotice(data.message, true);
    document.getElementById('vehiclesTableBody').innerHTML = data.data.map(v => `
        <tr>
            <td class="fw-bold">${v.vehicle_id}</td>
            <td>${v.owner_name}</td>
            <td class="fw-bold">${v.plate_number}</td>
            <td>${v.vehicle_type === 'EV'
                ? '<span class="pill-emerald">Xe điện</span>'
                : '<span class="pill-amber">Xe xăng</span>'}</td>
        </tr>
    `).join('');
}

async function handleCreateVehicle(e) {
    e.preventDefault();
    const resp = await fetch('/api/vehicles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            vehicle_id: document.getElementById('v_id').value.trim(),
            plate_number: document.getElementById('v_plate').value.trim(),
            vehicle_type: document.getElementById('v_type').value
        })
    });
    const data = await resp.json();
    showNotice(data.message, !resp.ok);
    if (resp.ok) loadVehicles();
}

async function loadStations() {
    const resp = await fetch('/api/stations');
    const data = await resp.json();
    if (!resp.ok) return showNotice(data.message, true);
    document.getElementById('stationsTableBody').innerHTML = data.data.map(st => `
        <tr>
            <td class="fw-bold">${st.station_id}</td>
            <td class="fw-bold emerald-text">${st.slot_id}</td>
            <td>${st.location_name} (Tầng ${st.floor})</td>
            <td>${st.charger_type}</td>
            <td><span class="pill-emerald">${st.connector_type}</span></td>
            <td class="fw-bold">${st.max_power_kw} kW</td>
            <td>${formatVND(st.idle_fee_rate)}</td>
            <td>${formatVND(st.violation_fee_rate)}</td>
            <td>${translateStatus(st.status)}</td>
        </tr>
    `).join('');
}

async function loadSlots() {
    const loc = document.getElementById('filterSlotLocation').value;
    const stype = document.getElementById('filterSlotType').value;
    const avail = document.getElementById('filterOnlyAvailable').checked;

    const url = `/api/slots?location_id=${encodeURIComponent(loc)}&slot_type=${encodeURIComponent(stype)}&only_available=${avail}`;
    const resp = await fetch(url);
    const data = await resp.json();
    if (!resp.ok) return showNotice(data.message, true);

    document.getElementById('slotSearchTimeBadge').textContent = 'Thời gian tìm kiếm: ' + data.query_time_ms + ' ms';

    document.getElementById('slotsTableBody').innerHTML = data.data.map(s => `
        <tr>
            <td class="fw-bold">${s.slot_id}</td>
            <td>${s.location_name}</td>
            <td>Tầng ${s.floor}</td>
            <td>${s.slot_type === 'Charging' ? '<span class="pill-emerald">Chỗ đỗ có sạc điện</span>' : '<span class="pill-slate">Chỗ đỗ tiêu chuẩn</span>'}</td>
            <td>${s.charger_type ? `${s.charger_type} (${s.connector_type} - ${s.max_power_kw} kW)` : 'Không lắp trụ sạc'}</td>
            <td>${translateStatus(s.status)}</td>
            <td>
                <div class="d-flex gap-1">
                    <button onclick="updateSlotStatus('${s.slot_id}', 'Available')" class="btn btn-secondary-dark btn-sm">Trống</button>
                    <button onclick="updateSlotStatus('${s.slot_id}', 'Occupied')" class="btn btn-secondary-dark btn-sm">Có xe</button>
                    <button onclick="updateSlotStatus('${s.slot_id}', 'Maintenance')" class="btn btn-secondary-dark btn-sm">Bảo trì</button>
                </div>
            </td>
        </tr>
    `).join('');
}

async function updateSlotStatus(slotId, status) {
    const resp = await fetch(`/api/slots/${slotId}/status`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status })
    });
    const data = await resp.json();
    showNotice(data.message, !resp.ok);
    if (resp.ok) loadSlots();
}

async function loadReservations() {
    const resp = await fetch('/api/reservations');
    const data = await resp.json();
    if (!resp.ok) return showNotice(data.message, true);
    document.getElementById('reservationsTableBody').innerHTML = data.data.map(r => `
        <tr>
            <td class="fw-bold">${r.reservation_id}</td>
            <td>${r.customer_name}</td>
            <td class="fw-bold">${r.plate_number} (${r.vehicle_type === 'EV' ? 'Xe điện' : 'Xe xăng'})</td>
            <td class="fw-bold">${r.slot_id} (${r.slot_type === 'Charging' ? 'Chỗ sạc' : 'Chỗ thường'})</td>
            <td>${formatDateTimeVI(r.start_time)}</td>
            <td>${formatDateTimeVI(r.end_time)}</td>
            <td class="fw-bold emerald-text">${formatVND(r.total_amount)}</td>
            <td>${translateStatus(r.payment_status)}</td>
        </tr>
    `).join('');
}

async function handleCreateReservation(e) {
    e.preventDefault();
    const payload = {
        reservation_id: document.getElementById('r_id').value.trim(),
        vehicle_id: document.getElementById('r_vehicle').value,
        slot_id: document.getElementById('r_slot').value,
        rate_id: document.getElementById('r_rate').value,
        start_time: document.getElementById('r_start').value,
        end_time: document.getElementById('r_end').value
    };
    const resp = await fetch('/api/reservations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    const data = await resp.json();
    showNotice(data.message, !resp.ok);
    if (resp.ok) {
        loadAvailableSlotsForBooking();
        loadReservations();
    }
}

async function loadSessions() {
    const resp = await fetch('/api/sessions');
    const data = await resp.json();
    if (!resp.ok) return showNotice(data.message, true);
    document.getElementById('sessionsTableBody').innerHTML = data.data.map(s => `
        <tr>
            <td class="fw-bold">${s.session_id}</td>
            <td class="fw-bold">${s.plate_number}</td>
            <td>${s.charging_station_id}</td>
            <td>${formatDateTimeVI(s.start_date_time)}</td>
            <td>${formatDateTimeVI(s.end_date_time)}</td>
            <td><span class="pill-emerald">${s.energy_consumed_kwh} kWh</span></td>
            <td>${formatVND(s.rate_per_kwh)}</td>
            <td class="fw-bold emerald-text">${formatVND(s.charging_cost)}</td>
        </tr>
    `).join('');
}

async function handleCreateSession(e) {
    e.preventDefault();
    const resp = await fetch('/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            reservation_id: document.getElementById('s_res_id').value.trim(),
            vehicle_id: document.getElementById('s_vehicle').value,
            charging_station_id: document.getElementById('s_station').value,
            rate_id: document.getElementById('s_rate').value,
            start_date_time: document.getElementById('s_start').value,
            end_date_time: document.getElementById('s_end').value
        })
    });
    const data = await resp.json();
    showNotice(data.message, !resp.ok);
    if (resp.ok) loadSessions();
}

async function loadBilling() {
    const resp = await fetch('/api/billing');
    const data = await resp.json();
    if (!resp.ok) return showNotice(data.message, true);

    document.getElementById('feesTableBody').innerHTML = data.fees.map(f => `
        <tr>
            <td class="fw-bold">${f.fee_id}</td>
            <td class="fw-bold">${f.reservation_id}</td>
            <td>${f.customer_name} (${f.plate_number})</td>
            <td>${f.fee_type === 'Violation'
                ? '<span class="pill-red">Phí vi phạm (Xe xăng đỗ chỗ sạc)</span>'
                : '<span class="pill-amber">Phí chiếm chỗ sau khi sạc</span>'}</td>
            <td>${f.excess_duration} phút</td>
            <td class="fw-bold text-danger">${formatVND(f.amount)}</td>
        </tr>
    `).join('') || '<tr><td colspan="6" class="text-center" style="color:var(--text-muted);">Không có khoản phí phát sinh.</td></tr>';

    document.getElementById('paymentsTableBody').innerHTML = data.payments.map(p => `
        <tr>
            <td class="fw-bold">${p.payment_id}</td>
            <td class="fw-bold">${p.reservation_id}</td>
            <td>${p.customer_name} (${p.plate_number})</td>
            <td>${formatDateTimeVI(p.payment_datetime)}</td>
            <td class="fw-bold emerald-text">${formatVND(p.amount)}</td>
            <td class="small" style="color:var(--text-muted);">${p.payment_method}</td>
            <td>${translateStatus(p.payment_status)}</td>
        </tr>
    `).join('') || '<tr><td colspan="7" class="text-center" style="color:var(--text-muted);">Chưa có giao dịch thanh toán.</td></tr>';

    const rateLabel = { 'Peak': 'Giờ cao điểm', 'Normal': 'Giờ bình thường', 'Off-Peak': 'Giờ thấp điểm' };
    const pRows = data.parking_rates.map(r => `
        <tr>
            <td class="fw-bold">${r.rate_id} (${rateLabel[r.rate_type] || r.rate_type} - Đỗ xe)</td>
            <td>${r.effective_from} → ${r.effective_to}</td>
            <td class="fw-bold emerald-text">${formatVND(r.price_per_hour)} / giờ</td>
        </tr>
    `).join('');
    const cRows = data.charging_rates.map(r => `
        <tr>
            <td class="fw-bold">${r.rate_id} (${rateLabel[r.rate_type] || r.rate_type} - Sạc điện)</td>
            <td>${r.effective_from} → ${r.effective_to}</td>
            <td class="fw-bold emerald-text">${formatVND(r.rate_per_kwh)} / kWh</td>
        </tr>
    `).join('');
    document.getElementById('ratesConfigBody').innerHTML = pRows + cRows;
}

async function handlePayInvoice(e) {
    e.preventDefault();
    const resp = await fetch('/api/payments/pay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            reservation_id: document.getElementById('pay_res_id').value.trim(),
            payment_method: document.getElementById('pay_method').value
        })
    });
    const data = await resp.json();
    showNotice(data.message, !resp.ok);
    if (resp.ok) loadBilling();
}

async function handleUpdateRate(e) {
    e.preventDefault();
    const resp = await fetch('/api/rates/update', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            kind: document.getElementById('cfg_kind').value,
            rate_id: document.getElementById('cfg_rate_id').value.trim(),
            value: document.getElementById('cfg_val').value
        })
    });
    const data = await resp.json();
    showNotice(data.message, !resp.ok);
    if (resp.ok) loadBilling();
}

window.onload = async () => {
    const resp = await fetch('/api/auth/me');
    const data = await resp.json();
    if (data.authenticated) {
        currentUser = data.user;
        enterApp();
    }
};
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def home_page():
    return render_template_string(FRONTEND_VI_HTML)


@app.errorhandler(404)
def page_not_found(e):
    html_404 = """
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <title>404 - Không tìm thấy trang</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {
                background: linear-gradient(180deg, #0b111e 0%, #1e293b 50%, #0b111e 100%);
                color: #f8fafc;
                font-family: sans-serif;
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            .box {
                background: rgba(15, 23, 42, 0.88);
                border: 1px solid rgba(148, 163, 184, 0.2);
                border-radius: 16px;
                padding: 40px;
                text-align: center;
                max-width: 460px;
            }
            .btn-emerald {
                background: #059669;
                color: #fff;
                text-decoration: none;
                padding: 10px 22px;
                border-radius: 10px;
                font-weight: 600;
                display: inline-block;
            }
        </style>
    </head>
    <body>
        <div class="box">
            <h1 style="font-size:56px;font-weight:800;color:#10b981;">404</h1>
            <h4 class="fw-bold mb-2">Không tìm thấy trang yêu cầu</h4>
            <p style="color:#94a3b8;" class="mb-4">Đường dẫn bạn truy cập không tồn tại trên hệ thống Smart City.</p>
            <a href="/" class="btn-emerald">Quay về Trang chủ</a>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_404), 404


if __name__ == "__main__":
    ensure_schema_ready()
    app.run(host="127.0.0.1", port=5000, debug=True)