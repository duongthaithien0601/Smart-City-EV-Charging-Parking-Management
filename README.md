# Smart City EV Charging & Parking Management System

## 👥 Team Members

1. **Dương Thái Thiện**
2. **Phan Nguyễn Tiến Vỹ**
3. **Nguyễn Thanh Tâm**

## 📖 Project Overview

The **Smart City EV Charging & Parking Management System** is a comprehensive database solution engineered to address the growing infrastructure demands of electric vehicles in modern urban environments. This project provides a robust backend architecture for managing EV charging stations, conventional parking spaces, user reservations, charging sessions, financial transactions, and dynamic pricing models.

The system empowers drivers to register their electric vehicles, search for available infrastructure, and secure parking slots for defined periods. By strictly recording the start and end times of every reservation and charging session, the database acts as a central source of truth for calculating accurate parking durations, energy consumption in kilowatt-hours (kWh), and corresponding financial fees.

## ⚙️ Core Business Logic & Features

To ensure operational efficiency and enforce city regulations, the database is designed around a strict set of business rules:

- **Smart Slot Allocation & Overlap Prevention:** The system distinguishes between regular parking slots and EV-equipped charging slots. Crucially, it employs time-interval logic to prevent any parking slot from being reserved by multiple customers during overlapping periods.
- **Exclusive EV Policies & Automated Penalties:** Charging slots are strictly reserved for Electric Vehicles. If the database detects a non-electric (Internal Combustion Engine - ICE) vehicle occupying an EV charging slot, it automatically triggers a parking violation fee.
- **Energy Tracking & Idle Fees:** When an EV utilizes a charging station, the system logs the session duration and total kWh consumed. If a vehicle remains parked in a charging slot beyond a specified grace period after charging is complete (or exceeds the maximum kWh limit), an idle parking fee is dynamically applied for the excess duration.
- **Dynamic Pricing Engine:** Parking fees and charging rates are not static; they fluctuate based on effective time periods (e.g., peak vs. off-peak hours). If a session crosses multiple pricing periods, the system calculates the exact applicable charge for each distinct period to generate the final total cost.

## 🏗️ Database Architecture & Modeling

The foundation of this project is built upon rigorous database design principles, normalized to 3NF/BCNF standards to ensure data integrity and eliminate redundancies.

The conceptual database model is thoroughly documented and visualized using **Mermaid syntax**, illustrating the complex cardinality and relationships between core system entities. Key structural components include:

- **`parkingLocation` & `Slot`:** Managing the hierarchical structure of physical parking areas and specific spaces.
- **`ChargingStation`:** Tracking hardware specifications, connector types, and maximum power capacities.
- **`ParkingRate` & `Payment`:** Handling the dynamic calculation of total fees (Parking Fee + Charging Fee + Penalties) and maintaining immutable financial records.
