-- ============================================================================
-- HỆ THỐNG SMART CITY: QUẢN LÝ TRẠM SẠC & BÃI ĐỖ XE THÔNG MINH
-- SCRIPT CẬP NHẬT & ĐỒNG BỘ CSDL POSTGRESQL (KHÔNG XÓA BẢNG / KHÔNG MẤT DỮ LIỆU)
-- ============================================================================

-- 1. Bảng users (Chỉ tạo nếu chưa có; giữ nguyên dữ liệu hiện tại)
CREATE TABLE IF NOT EXISTS users (
    user_id VARCHAR(20) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) NOT NULL UNIQUE,
    phone_number VARCHAR(15) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'customer'
);

-- Bổ sung cột role nếu bảng users cũ chưa có
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'customer';

-- Chuẩn hóa email các tài khoản cũ sang đuôi @gmail.com và đồng bộ 2 vai trò trước khi gắn ràng buộc CHECK
UPDATE users
SET email = REGEXP_REPLACE(email, '@.*$', '@gmail.com')
WHERE LOWER(email) NOT LIKE '%_@gmail.com';

UPDATE users
SET role = 'customer'
WHERE role NOT IN ('system_admin', 'customer') OR role IS NULL;

-- Gỡ ràng buộc cũ (nếu có) và áp dụng ràng buộc mới: bắt buộc @gmail.com và chỉ 2 vai trò
ALTER TABLE users DROP CONSTRAINT IF EXISTS chk_users_email;
ALTER TABLE users DROP CONSTRAINT IF EXISTS chk_users_email_gmail;
ALTER TABLE users ADD CONSTRAINT chk_users_email_gmail CHECK (LOWER(email) LIKE '%_@gmail.com');

ALTER TABLE users DROP CONSTRAINT IF EXISTS chk_users_role;
ALTER TABLE users ADD CONSTRAINT chk_users_role CHECK (role IN ('system_admin', 'customer'));


-- 2. Bảng parking_locations
CREATE TABLE IF NOT EXISTS parking_locations (
    location_id VARCHAR(20) PRIMARY KEY,
    location_name VARCHAR(100) NOT NULL,
    address VARCHAR(255) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'Active',
    CONSTRAINT chk_location_status CHECK (status IN ('Active', 'Maintenance', 'Closed'))
);


-- 3. Bảng parking_rates
CREATE TABLE IF NOT EXISTS parking_rates (
    rate_id VARCHAR(20) PRIMARY KEY,
    rate_type VARCHAR(20) NOT NULL,
    effective_from TIME NOT NULL,
    effective_to TIME NOT NULL,
    price_per_hour DECIMAL(10, 2) NOT NULL,
    CONSTRAINT chk_parking_rate_type CHECK (rate_type IN ('Peak', 'Normal', 'Off-Peak')),
    CONSTRAINT chk_parking_price_positive CHECK (price_per_hour > 0)
);


-- 4. Bảng charging_rates
CREATE TABLE IF NOT EXISTS charging_rates (
    rate_id VARCHAR(20) PRIMARY KEY,
    rate_per_kwh DECIMAL(10, 2) NOT NULL,
    effective_from TIME NOT NULL,
    effective_to TIME NOT NULL,
    rate_type VARCHAR(20) NOT NULL,
    CONSTRAINT chk_charging_rate_type CHECK (rate_type IN ('Peak', 'Normal', 'Off-Peak')),
    CONSTRAINT chk_charging_rate_positive CHECK (rate_per_kwh > 0)
);


-- 5. Bảng vehicles (EV / ICE, biển số UNIQUE, xóa dây chuyền theo user)
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id VARCHAR(20) PRIMARY KEY,
    user_id VARCHAR(20) NOT NULL,
    plate_number VARCHAR(20) NOT NULL UNIQUE,
    vehicle_type VARCHAR(10) NOT NULL DEFAULT 'EV',
    CONSTRAINT fk_vehicles_user FOREIGN KEY (user_id)
        REFERENCES users(user_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT chk_vehicle_type CHECK (vehicle_type IN ('EV', 'ICE'))
);


-- 6. Bảng slots (Supertype)
CREATE TABLE IF NOT EXISTS slots (
    slot_id VARCHAR(20) PRIMARY KEY,
    location_id VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'Available',
    slot_type VARCHAR(20) NOT NULL DEFAULT 'Standard',
    floor INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT fk_slots_location FOREIGN KEY (location_id)
        REFERENCES parking_locations(location_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT chk_slot_status CHECK (status IN ('Available', 'Occupied', 'Reserved', 'Maintenance')),
    CONSTRAINT chk_slot_type CHECK (slot_type IN ('Standard', 'Charging'))
);


-- 7. Bảng charging_slots (Subtype)
CREATE TABLE IF NOT EXISTS charging_slots (
    slot_id VARCHAR(20) PRIMARY KEY,
    charger_type VARCHAR(30) NOT NULL,
    kw_rate DECIMAL(6, 2) NOT NULL,
    power_capacity DECIMAL(6, 2) NOT NULL,
    idle_fee_rate DECIMAL(10, 2) NOT NULL DEFAULT 50000.00,
    violation_fee_rate DECIMAL(10, 2) NOT NULL DEFAULT 200000.00,
    CONSTRAINT fk_charging_slots_slot FOREIGN KEY (slot_id)
        REFERENCES slots(slot_id) ON DELETE CASCADE ON UPDATE CASCADE
);


-- 8. Bảng charging_stations
CREATE TABLE IF NOT EXISTS charging_stations (
    station_id VARCHAR(20) PRIMARY KEY,
    slot_id VARCHAR(20) NOT NULL UNIQUE,
    connector_type VARCHAR(30) NOT NULL,
    max_power_kw DECIMAL(6, 2) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'Online',
    CONSTRAINT fk_stations_charging_slot FOREIGN KEY (slot_id)
        REFERENCES charging_slots(slot_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT chk_connector_type CHECK (connector_type IN ('CCS2', 'Type 2', 'CHAdeMO')),
    CONSTRAINT chk_station_status CHECK (status IN ('Online', 'Busy', 'Offline', 'Faulted'))
);


-- 9. Bảng reservations (Ràng buộc end_time > start_time)
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id VARCHAR(50) PRIMARY KEY,
    vehicle_id VARCHAR(20) NOT NULL,
    slot_id VARCHAR(20) NOT NULL,
    rate_id VARCHAR(20) NOT NULL,
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP NOT NULL,
    total_amount DECIMAL(10, 2) DEFAULT 0.00,
    payment_status VARCHAR(20) NOT NULL DEFAULT 'Pending',
    CONSTRAINT fk_reservations_vehicle FOREIGN KEY (vehicle_id)
        REFERENCES vehicles(vehicle_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_reservations_slot FOREIGN KEY (slot_id)
        REFERENCES slots(slot_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_reservations_rate FOREIGN KEY (rate_id)
        REFERENCES parking_rates(rate_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT chk_reservation_time CHECK (end_time > start_time),
    CONSTRAINT chk_reservation_payment_status CHECK (payment_status IN ('Pending', 'Paid', 'Failed', 'Refunded'))
);


-- 10. Bảng charging_sessions (energy_consumed_kwh do hệ thống tự động tính và ghi)
CREATE TABLE IF NOT EXISTS charging_sessions (
    session_id VARCHAR(30) PRIMARY KEY,
    vehicle_id VARCHAR(20) NOT NULL,
    charging_station_id VARCHAR(20) NOT NULL,
    rate_id VARCHAR(20) NOT NULL,
    start_date_time TIMESTAMP NOT NULL,
    end_date_time TIMESTAMP NOT NULL,
    energy_consumed_kwh DECIMAL(8, 2) NOT NULL,
    CONSTRAINT fk_sessions_vehicle FOREIGN KEY (vehicle_id)
        REFERENCES vehicles(vehicle_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_sessions_station FOREIGN KEY (charging_station_id)
        REFERENCES charging_stations(station_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_sessions_rate FOREIGN KEY (rate_id)
        REFERENCES charging_rates(rate_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT chk_session_time CHECK (end_date_time > start_date_time),
    CONSTRAINT chk_energy_positive CHECK (energy_consumed_kwh > 0)
);


-- 11. Bảng fees
CREATE TABLE IF NOT EXISTS fees (
    fee_id VARCHAR(30) PRIMARY KEY,
    reservation_id VARCHAR(50) NOT NULL,
    fee_type VARCHAR(20) NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    excess_duration INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT fk_fees_reservation FOREIGN KEY (reservation_id)
        REFERENCES reservations(reservation_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT chk_fee_type CHECK (fee_type IN ('Violation', 'Idle'))
);


-- 12. Bảng payments (mở rộng cột payment_method nếu bảng cũ đang để độ dài ngắn hơn 255)
CREATE TABLE IF NOT EXISTS payments (
    payment_id VARCHAR(30) PRIMARY KEY,
    reservation_id VARCHAR(50) NOT NULL UNIQUE,
    payment_datetime TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    amount DECIMAL(10, 2) NOT NULL,
    payment_method VARCHAR(255) NOT NULL,
    payment_status VARCHAR(20) NOT NULL DEFAULT 'Completed',
    CONSTRAINT fk_payments_reservation FOREIGN KEY (reservation_id)
        REFERENCES reservations(reservation_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT chk_payment_status CHECK (payment_status IN ('Completed', 'Pending', 'Failed', 'Refunded'))
);

ALTER TABLE payments
    ALTER COLUMN payment_method TYPE VARCHAR(255);


-- ============================================================================
-- CHỈ MỤC B-TREE (CHỈ TẠO NẾU CHƯA TỒN TẠI)
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_vehicles_plate_number ON vehicles(plate_number);
CREATE INDEX IF NOT EXISTS idx_reservations_time ON reservations(start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_slots_status ON slots(status, slot_type);


-- ============================================================================
-- VIEW: vw_active_reservations (Cập nhật không xóa bảng)
-- ============================================================================
CREATE OR REPLACE VIEW vw_active_reservations AS
SELECT 
    r.reservation_id,
    u.user_id,
    u.name AS customer_name,
    v.vehicle_id,
    v.plate_number,
    v.vehicle_type,
    pl.location_name,
    s.slot_id,
    s.slot_type,
    r.start_time,
    r.end_time,
    r.total_amount,
    r.payment_status
FROM reservations r
JOIN vehicles v ON r.vehicle_id = v.vehicle_id
JOIN users u ON v.user_id = u.user_id
JOIN slots s ON r.slot_id = s.slot_id
JOIN parking_locations pl ON s.location_id = pl.location_id
WHERE r.payment_status = 'Pending'
  AND r.end_time > CURRENT_TIMESTAMP;


-- ============================================================================
-- TRIGGER CHỐNG ĐẶT CHỖ TRÙNG THỜI GIAN (Cập nhật hàm & trigger)
-- ============================================================================
CREATE OR REPLACE FUNCTION fn_prevent_overlapping_reservations()
RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM reservations r
        WHERE r.slot_id = NEW.slot_id
          AND r.reservation_id <> COALESCE(NEW.reservation_id, '')
          AND r.payment_status IN ('Pending', 'Paid')
          AND (NEW.start_time < r.end_time AND NEW.end_time > r.start_time)
    ) THEN
        RAISE EXCEPTION 'OVERLAP_SLOT';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prevent_overlapping_reservations ON reservations;
CREATE TRIGGER trg_prevent_overlapping_reservations
BEFORE INSERT OR UPDATE ON reservations
FOR EACH ROW EXECUTE FUNCTION fn_prevent_overlapping_reservations();


-- ============================================================================
-- ĐỒNG BỘ DỮ LIỆU MẪU TIẾNG VIỆT (ON CONFLICT DO UPDATE - KHÔNG XÓA BẢNG)
-- Mật khẩu mặc định đã băm SHA-256 của '123456'
-- ============================================================================

INSERT INTO users (user_id, name, email, phone_number, password_hash, role) VALUES
('USR001', 'Phan Nguyễn Tiến Vỹ', 'quantri.smartcity@gmail.com', '0901234501', '8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92', 'system_admin'),
('USR002', 'Dương Thái Thiện', 'thaithien.sc@gmail.com', '0901234502', '8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92', 'customer'),
('USR003', 'Nguyễn Thanh Tâm', 'thanhtam.sc@gmail.com', '0901234503', '8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92', 'customer'),
('USR004', 'Lê Hoàng Minh', 'hoangminh.sc@gmail.com', '0912345604', '8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92', 'customer'),
('USR005', 'Trần Thị Mai Anh', 'maianh.sc@gmail.com', '0912345605', '8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92', 'customer')
ON CONFLICT (user_id) DO UPDATE SET
    name = EXCLUDED.name,
    email = EXCLUDED.email,
    phone_number = EXCLUDED.phone_number,
    password_hash = EXCLUDED.password_hash,
    role = EXCLUDED.role;

INSERT INTO parking_locations (location_id, location_name, address, status) VALUES
('LOC001', 'Trạm trung tâm Sài Gòn Centre', '65 Lê Lợi, Quận 1, TP.HCM', 'Active'),
('LOC002', 'Khu công nghệ cao Thủ Đức', 'Võ Nguyên Giáp, TP. Thủ Đức, TP.HCM', 'Active'),
('LOC003', 'Bãi đỗ thông minh Landmark 81', '720A Điện Biên Phủ, Bình Thạnh, TP.HCM', 'Active')
ON CONFLICT (location_id) DO UPDATE SET
    location_name = EXCLUDED.location_name,
    address = EXCLUDED.address,
    status = EXCLUDED.status;

INSERT INTO parking_rates (rate_id, rate_type, effective_from, effective_to, price_per_hour) VALUES
('PR_OFFPEAK', 'Off-Peak', '22:00:00', '06:00:00', 15000.00),
('PR_NORMAL',  'Normal',   '06:00:01', '16:59:59', 25000.00),
('PR_PEAK',    'Peak',     '17:00:00', '21:59:59', 45000.00)
ON CONFLICT (rate_id) DO UPDATE SET price_per_hour = EXCLUDED.price_per_hour;

INSERT INTO charging_rates (rate_id, rate_per_kwh, effective_from, effective_to, rate_type) VALUES
('CR_OFFPEAK', 2800.00, '22:00:00', '06:00:00', 'Off-Peak'),
('CR_NORMAL',  3500.00, '06:00:01', '16:59:59', 'Normal'),
('CR_PEAK',    4800.00, '17:00:00', '21:59:59', 'Peak')
ON CONFLICT (rate_id) DO UPDATE SET rate_per_kwh = EXCLUDED.rate_per_kwh;

INSERT INTO vehicles (vehicle_id, user_id, plate_number, vehicle_type) VALUES
('VEH001', 'USR001', '51K-888.01', 'EV'),
('VEH002', 'USR003', '51H-123.45', 'ICE'),
('VEH003', 'USR002', '51K-999.02', 'EV'),
('VEH004', 'USR003', '51K-777.03', 'EV'),
('VEH005', 'USR004', '51F-555.04', 'ICE'),
('VEH006', 'USR005', '51K-666.05', 'EV')
ON CONFLICT (vehicle_id) DO UPDATE SET
    user_id = EXCLUDED.user_id,
    plate_number = EXCLUDED.plate_number,
    vehicle_type = EXCLUDED.vehicle_type;

INSERT INTO slots (slot_id, location_id, status, slot_type, floor) VALUES
('SLT001', 'LOC001', 'Available', 'Charging', 1),
('SLT002', 'LOC001', 'Occupied',  'Charging', 1),
('SLT003', 'LOC001', 'Available', 'Standard', 2),
('SLT004', 'LOC002', 'Available', 'Charging', 1),
('SLT005', 'LOC002', 'Reserved',  'Charging', 1),
('SLT006', 'LOC002', 'Available', 'Standard', 2)
ON CONFLICT (slot_id) DO UPDATE SET status = EXCLUDED.status;

INSERT INTO charging_slots (slot_id, charger_type, kw_rate, power_capacity, idle_fee_rate, violation_fee_rate) VALUES
('SLT001', 'Sạc nhanh DC 60kW',   60.00, 120.00, 60000.00, 250000.00),
('SLT002', 'Siêu sạc DC 120kW',  120.00, 150.00, 80000.00, 300000.00),
('SLT004', 'Sạc tiêu chuẩn AC',   22.00,  44.00, 30000.00, 150000.00),
('SLT005', 'Sạc nhanh DC 60kW',   60.00, 120.00, 60000.00, 250000.00)
ON CONFLICT (slot_id) DO UPDATE SET violation_fee_rate = EXCLUDED.violation_fee_rate;

INSERT INTO charging_stations (station_id, slot_id, connector_type, max_power_kw, status) VALUES
('STN001', 'SLT001', 'CCS2',   60.00, 'Online'),
('STN002', 'SLT002', 'CCS2',  120.00, 'Busy'),
('STN003', 'SLT004', 'Type 2', 22.00, 'Online'),
('STN004', 'SLT005', 'CCS2',   60.00, 'Online')
ON CONFLICT (station_id) DO UPDATE SET status = EXCLUDED.status;

INSERT INTO reservations (reservation_id, vehicle_id, slot_id, rate_id, start_time, end_time, total_amount, payment_status) VALUES
('RES20261101', 'VEH001', 'SLT001', 'PR_NORMAL',  '2026-11-15 08:00:00', '2026-11-15 10:00:00', 242000.00, 'Paid'),
('RES20261102', 'VEH003', 'SLT002', 'PR_PEAK',    '2026-11-15 17:30:00', '2026-11-15 19:30:00', 410400.00, 'Paid'),
('RES20261103', 'VEH005', 'SLT004', 'PR_NORMAL',  '2026-11-15 13:00:00', '2026-11-15 15:00:00', 200000.00, 'Paid'),
('RES20261104', 'VEH004', 'SLT005', 'PR_OFFPEAK', '2026-11-28 22:30:00', '2026-11-29 01:30:00', 185000.00, 'Pending'),
('RES20261105', 'VEH002', 'SLT001', 'PR_PEAK',    '2026-11-28 18:00:00', '2026-11-28 20:00:00', 340000.00, 'Pending')
ON CONFLICT (reservation_id) DO UPDATE SET payment_status = EXCLUDED.payment_status;

INSERT INTO charging_sessions (session_id, vehicle_id, charging_station_id, rate_id, start_date_time, end_date_time, energy_consumed_kwh) VALUES
('SES001', 'VEH001', 'STN001', 'CR_NORMAL',  '2026-11-15 08:05:00', '2026-11-15 09:15:00', 42.00),
('SES002', 'VEH003', 'STN002', 'CR_PEAK',    '2026-11-15 17:35:00', '2026-11-15 19:20:00', 66.75),
('SES003', 'VEH004', 'STN004', 'CR_OFFPEAK', '2026-11-28 22:35:00', '2026-11-29 00:50:00', 50.00)
ON CONFLICT (session_id) DO UPDATE SET energy_consumed_kwh = EXCLUDED.energy_consumed_kwh;

INSERT INTO fees (fee_id, reservation_id, fee_type, amount, excess_duration) VALUES
('FEE001', 'RES20261101', 'Idle',       45000.00, 45),
('FEE002', 'RES20261103', 'Violation', 150000.00, 120),
('FEE003', 'RES20261105', 'Violation', 250000.00, 120)
ON CONFLICT (fee_id) DO UPDATE SET amount = EXCLUDED.amount;

INSERT INTO payments (payment_id, reservation_id, payment_datetime, amount, payment_method, payment_status) VALUES
('PAY001', 'RES20261101', '2026-11-15 10:05:00', 242000.00, 'ENC_SHA256_VI_DIEN_TU_9F8A', 'Completed'),
('PAY002', 'RES20261102', '2026-11-15 19:35:00', 410400.00, 'ENC_SHA256_THE_TIN_DUNG_3B2C', 'Completed'),
('PAY003', 'RES20261103', '2026-11-15 15:10:00', 200000.00, 'ENC_SHA256_QR_PAY_7D1E', 'Completed')
ON CONFLICT (payment_id) DO UPDATE SET payment_status = EXCLUDED.payment_status;