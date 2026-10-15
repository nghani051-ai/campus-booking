# CampusSlot: Smart Campus Resource & Room Booking System

Local web app for booking rooms, halls, lab gear and compute.
Python standard library only (http.server + SQLite). **No pip install, no internet needed.**

## Run
    python app.py          # opens http://localhost:8000  (use python3 on Mac/Linux)
    python app.py 9000     # custom port

Demo logins: `admin / admin123`, `nouman / pass123`, `diya / pass123` (or create a student account).
Delete `campus.db` to reset to fresh demo data.

## Requirements covered
| Requirement | Where |
|---|---|
| Resource directory (capacity, specs, guidelines) | Resources tab; admin can add resources |
| Slot selector + conflict checks | Timetable picker in the booking window; server rejects any overlap with pending/approved bookings in one DB transaction |
| Approval workflow with notes | Admin > Approvals (approve/reject, note required on reject) |
| Personal dashboard | My bookings: upcoming/past, status, admin notes, cancel |
| History log & alerts | Admin > History log (audit trail); toasts + bell notifications on status changes |

## Extra features
- **Weekly repeat bookings** (up to 8 weeks), all-or-nothing: one clash rejects the whole series.
- **Auto-expiry:** pending requests with no decision by their start time are closed and the requester is notified.
- **Smart clash hints:** when a slot is taken, the app suggests the nearest free slots of the same length.
- **CSV export** of the admin history log.

## Components
- `app.py`: HTTP server, JSON API, SQLite schema + seed data, validation, conflict logic
- `static/index.html`: single-page UI (vanilla JS, no libraries)
- `campus.db`: created on first run (users, resources, bookings, history, notifications)

## Design notes
- Overlap rule: `new_start < existing_end AND new_end > existing_start`, so back-to-back bookings are allowed.
- Pending requests also hold the slot; rejecting or cancelling frees it.
- Admin bookings are auto-approved. Passwords are salted PBKDF2 hashes. Booking purpose is visible only to owner and admins.
- Bookable window: 08:00-20:00 in 30-minute slots.
