-- ============================================================================
-- HỆ THỐNG SMART CITY: QUẢN LÝ TRẠM SẠC & BÃI ĐỖ XE THÔNG MINH
-- ============================================================================

-- 1. Bảng users 
CREATE TABLE IF NOT EXISTS users (
    user_id VARCHAR(20) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) NOT NULL UNIQUE,
    phone_number VARCHAR(15) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'customer',
    CONSTRAINT chk_users_email_gmail CHECK (LOWER(email) LIKE '%_@gmail.com'),
    CONSTRAINT chk_users_role CHECK (role IN ('system_admin', 'customer'))
);

-- 2. Bảng parking_locations
CREATE TABLE IF NOT EXISTS parking_locations (
    location_id VARCHAR(20) PRIMARY KEY,
    location_name VARCHAR(100) NOT NULL,
    address VARCHAR(255) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'Active',
    CONSTRAINT chk_location_status CHECK (status IN ('Active', 'Maintenance', 'Closed'))
);
CREATE TABLE IF NOT EXISTS violation_rates (
    rate_id VARCHAR(20) PRIMARY KEY,
    location_id VARCHAR(20) NOT NULL REFERENCES parking_locations(location_id) ON DELETE CASCADE ON UPDATE CASCADE,
    amount DECIMAL(10, 2) NOT NULL CHECK (amount > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 3. Bảng parking_rates
CREATE TABLE IF NOT EXISTS parking_rates (
    rate_id VARCHAR(20) PRIMARY KEY,
    location_id VARCHAR(20) NOT NULL REFERENCES parking_locations(location_id) ON DELETE CASCADE ON UPDATE CASCADE,
    service_type VARCHAR(20) NOT NULL DEFAULT 'parking',
    rate_type VARCHAR(20) NOT NULL,
    effective_from TIME NOT NULL,
    effective_to TIME NOT NULL,
    price_per_hour DECIMAL(10, 2) NOT NULL,
    CONSTRAINT chk_parking_rate_type CHECK (rate_type IN ('Peak', 'Normal', 'Off-Peak')),
    CONSTRAINT chk_parking_price_positive CHECK (price_per_hour > 0),
    CONSTRAINT chk_parking_service_type CHECK (service_type IN ('parking', 'both'))
);

-- 4. Bảng charging_rates
CREATE TABLE IF NOT EXISTS charging_rates (
    rate_id VARCHAR(20) PRIMARY KEY,
    location_id VARCHAR(20) NOT NULL REFERENCES parking_locations(location_id) ON DELETE CASCADE ON UPDATE CASCADE,
    rate_per_kwh DECIMAL(10, 2) NOT NULL,
    effective_from TIME NOT NULL,
    effective_to TIME NOT NULL,
    rate_type VARCHAR(20) NOT NULL,
    CONSTRAINT chk_charging_rate_type CHECK (rate_type IN ('Peak', 'Normal', 'Off-Peak')),
    CONSTRAINT chk_charging_rate_positive CHECK (rate_per_kwh > 0)
);

-- 5. Bảng vehicles 
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id VARCHAR(20) PRIMARY KEY,
    user_id VARCHAR(20) NOT NULL,
    plate_number VARCHAR(20) NOT NULL UNIQUE,
    vehicle_type VARCHAR(10) NOT NULL DEFAULT 'EV',
    CONSTRAINT fk_vehicles_user FOREIGN KEY (user_id)
        REFERENCES users(user_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT chk_vehicle_type CHECK (vehicle_type IN ('EV', 'ICE'))
);

-- 6. Bảng slots 
CREATE TABLE IF NOT EXISTS slots (
    slot_id VARCHAR(20) PRIMARY KEY,
    location_id VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'Available',
    slot_type VARCHAR(20) NOT NULL DEFAULT 'Standard',
    position INTEGER NOT NULL DEFAULT 1 CHECK (position > 0),
    CONSTRAINT fk_slots_location FOREIGN KEY (location_id)
        REFERENCES parking_locations(location_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT chk_slot_status CHECK (status IN ('Available', 'Occupied', 'Reserved', 'Maintenance')),
    CONSTRAINT chk_slot_type CHECK (slot_type IN ('Standard', 'Charging')),
    CONSTRAINT uq_slots_location_position UNIQUE (location_id, position)
);

-- 7. Bảng charging_slots
CREATE TABLE IF NOT EXISTS charging_slots (
    slot_id VARCHAR(20) PRIMARY KEY,
    charger_type VARCHAR(30) NOT NULL,
    kw_rate INTEGER NOT NULL CHECK (kw_rate > 0),
    CONSTRAINT fk_charging_slots_slot FOREIGN KEY (slot_id)
        REFERENCES slots(slot_id) ON DELETE CASCADE ON UPDATE CASCADE
);

-- 8. Bảng charging_stations
CREATE TABLE IF NOT EXISTS charging_stations (
    station_id VARCHAR(20) PRIMARY KEY,
    slot_id VARCHAR(20) NOT NULL UNIQUE,
    status VARCHAR(20) NOT NULL DEFAULT 'Online',
    CONSTRAINT fk_stations_charging_slot FOREIGN KEY (slot_id)
        REFERENCES charging_slots(slot_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT chk_station_status CHECK (status IN ('Online', 'Busy', 'Offline', 'Faulted'))
);

-- 9. Bảng reservations (Ràng buộc end_time > start_time)
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id VARCHAR(50) PRIMARY KEY,
    vehicle_id VARCHAR(20) NOT NULL,
    slot_id VARCHAR(20) NOT NULL,
    rate_id VARCHAR(20),
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    total_amount DECIMAL(10, 2) DEFAULT 0.00,
    vehicle_departed_at TIMESTAMPTZ,
    payment_status VARCHAR(20) NOT NULL DEFAULT 'Pending',
    CONSTRAINT fk_reservations_vehicle FOREIGN KEY (vehicle_id)
        REFERENCES vehicles(vehicle_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_reservations_slot FOREIGN KEY (slot_id)
        REFERENCES slots(slot_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_reservations_rate FOREIGN KEY (rate_id)
        REFERENCES parking_rates(rate_id) ON DELETE SET NULL ON UPDATE CASCADE,
    CONSTRAINT chk_reservation_time CHECK (end_time > start_time),
    CONSTRAINT chk_reservation_payment_status CHECK (payment_status IN ('Pending', 'Paid', 'Failed', 'Refunded'))
);

-- 10. Bảng charging_sessions (energy_consumed_kwh do hệ thống tự động tính và ghi)
CREATE TABLE IF NOT EXISTS charging_sessions (
    session_id VARCHAR(30) PRIMARY KEY,
    vehicle_id VARCHAR(20) NOT NULL,
    charging_station_id VARCHAR(20) NOT NULL,
    rate_id VARCHAR(20),
    start_date_time TIMESTAMPTZ NOT NULL,
    end_date_time TIMESTAMPTZ,
    energy_consumed_kwh DECIMAL(8, 2),
    session_status VARCHAR(20) NOT NULL DEFAULT 'Completed',
    reservation_id VARCHAR(50),
    vehicle_departed_at TIMESTAMPTZ,
    CONSTRAINT fk_sessions_vehicle FOREIGN KEY (vehicle_id)
        REFERENCES vehicles(vehicle_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_sessions_station FOREIGN KEY (charging_station_id)
        REFERENCES charging_stations(station_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_sessions_rate FOREIGN KEY (rate_id)
        REFERENCES charging_rates(rate_id) ON DELETE SET NULL ON UPDATE CASCADE,
    CONSTRAINT chk_session_time CHECK (end_date_time IS NULL OR end_date_time > start_date_time),
    CONSTRAINT chk_energy_positive CHECK (energy_consumed_kwh IS NULL OR energy_consumed_kwh > 0),
    CONSTRAINT chk_session_status CHECK (session_status IN ('Charging', 'Completed')),
    CONSTRAINT fk_session_reservation FOREIGN KEY (reservation_id)
        REFERENCES reservations(reservation_id) ON DELETE SET NULL ON UPDATE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_session_per_vehicle
    ON charging_sessions(vehicle_id) WHERE session_status = 'Charging';
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_session_per_station
    ON charging_sessions(charging_station_id) WHERE session_status = 'Charging';

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

-- 12. Bảng payments 
CREATE TABLE IF NOT EXISTS payments (
    payment_id VARCHAR(30) PRIMARY KEY,
    reservation_id VARCHAR(50) NOT NULL UNIQUE,
    payment_datetime TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    amount DECIMAL(10, 2) NOT NULL,
    payment_method VARCHAR(255) NOT NULL,
    payment_status VARCHAR(20) NOT NULL DEFAULT 'Completed',
    CONSTRAINT fk_payments_reservation FOREIGN KEY (reservation_id)
        REFERENCES reservations(reservation_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT chk_payment_status CHECK (payment_status IN ('Completed', 'Pending', 'Failed', 'Refunded'))
);

-- 13. Bảng payment_items
CREATE TABLE IF NOT EXISTS payment_items (
    payment_item_id VARCHAR(50) PRIMARY KEY,
    payment_id VARCHAR(30) NOT NULL REFERENCES payments(payment_id) ON DELETE CASCADE ON UPDATE CASCADE,
    item_type VARCHAR(20) NOT NULL CHECK (item_type IN ('parking', 'charging', 'violation')),
    amount DECIMAL(12, 2) NOT NULL,
    CONSTRAINT uq_payment_items_payment_type UNIQUE (payment_id, item_type)
);

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

-- Tạo hoặc cập nhật tài khoản quản trị mặc định.
INSERT INTO users (user_id, name, email, phone_number, password_hash, role) VALUES
('USR001', 'Quản trị Smart City', 'quantrismartcity@gmail.com', '0901234501', '8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92', 'system_admin')
ON CONFLICT (user_id) DO UPDATE SET
    name = EXCLUDED.name,
    email = EXCLUDED.email,
    phone_number = EXCLUDED.phone_number,
    role = EXCLUDED.role;
