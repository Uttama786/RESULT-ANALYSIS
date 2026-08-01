# 🎓 VTU Result Analysis & Automated Crawling Hub

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-18-61DAFB?style=for-the-badge&logo=react&logoColor=black)](https://react.dev)
[![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-3.0-38B2AC?style=for-the-badge&logo=tailwind-css&logoColor=white)](https://tailwindcss.com)
[![Render](https://img.shields.io/badge/Render-Deployed-46E3B7?style=for-the-badge&logo=render&logoColor=black)](https://render.com)

A full-stack, enterprise-grade automated academic intelligence platform designed to crawl, parse, analyze, and visualize Visvesvaraya Technological University (VTU) examination results for engineering institutions. Built for **Yenepoya Institute of Technology (YIT), Moodbidri, Mangaluru**.

---

## ✨ Key Features

### ⚡ Dual-Mode Crawling Engine
- **Ultra-Fast Direct HTTP Engine**: Powered by `requests.Session` with custom TLS adapters to crawl and parse student semester results in sub-second execution speeds per USN.
- **Selenium Chrome WebDriver Engine**: Automated browser fallback equipped with headless navigation for JS-heavy VTU result portals.
- **Real-Time WebSocket Streaming**: Stream live crawling status, active USN metrics, console execution logs, and CAPTCHAs directly to the React dashboard.

### 🤖 Intelligent CAPTCHA Solver
- **Auto-OCR Recognition**: Integrated PIL image binarization and Tesseract OCR to automatically solve 6-character VTU CAPTCHAs.
- **Interactive Manual Fallback & Refresh**: Base64 CAPTCHA screenshot streaming to the web UI with manual entry and instant image refresh options.

### 📊 Appeared Students Method (Accurate Pass/Fail Stats)
Calculates performance metrics strictly based on students who **appeared** for examinations:
- **Total Registered**: All USNs in the input range.
- **Absent ($AB$)**: Count of students absent for exams. Absent students are **never** counted as failed.
- **Appeared**: $\text{Appeared} = \text{Total Registered} - \text{Absent}$
- **Passed**: Appeared students with `PASS` status.
- **Failed**: Appeared students with `FAIL` status.
- **Pass Percentage**: $\text{Pass \%} = \left(\frac{\text{Passed}}{\text{Appeared}}\right) \times 100$
- **Fail Percentage**: $\text{Fail \%} = \left(\frac{\text{Failed}}{\text{Appeared}}\right) \times 100$

### 📈 Dynamic Analytics Dashboard
- **ApexCharts Visualizations**: Visual donut ratios for Passed / Failed / Absent breakdown, subject-wise pass rates, and score distributions.
- **Student Directory Table**: Interactive tabular list with live search, class filters (`FIRST CLASS WITH DISTINCTION`, `FIRST CLASS`, `SECOND CLASS`, `PASS`, `FAIL`, `ABSENT`), and subject grade popups.
- **Subject Performance Metrics**: Detailed subject-by-subject breakdown displaying internal, external, total score averages, and fail percentages.

### 📂 Multi-Sheet Automated Excel Export (`.xlsx`)
Generates color-coded OpenPyXL Excel reports containing:
1. **Overall Results**: Summary card metrics (Appeared, Absent, Pass %, Fail %), overall statistics, and complete student score directory.
2. **Subject Performance**: Subject-wise statistics, average marks, and pass/fail distributions.
3. **Class Toppers**: Ranked list of department toppers sorted by total marks and percentage.
4. **Remedial & Backlog Directory**: Filtered list of failed/absent students needing re-examination.

### 💾 Zero-Loss Persistent Storage (Render Compatible)
- **Database BLOB Archival**: Stores Excel reports directly as binary `BLOB`/`BYTEA` records inside PostgreSQL / SQLite databases.
- **Cloud Storage Integration**: Optional Cloudinary cloud storage upload for HTTPS URL delivery.
- **Render Server Restart Proof**: Reports remain downloadable forever via `/api/history/download/{id}` even when Render's ephemeral container filesystem resets on server sleep or redeploy.

### 👥 Multi-User Concurrent Session Isolation
- **Independent Session Tokens**: Every login generates a unique cryptographically random token (`secrets.token_hex(24)`). Multiple faculty members can log in simultaneously using shared credentials (`UttamBhise` / `#Uttama207`) without interfering with each other.
- **Isolated Workspace Folders**: Each session runs in its own isolated temporary directory (`backend/exports/session_{session_id}/`).
- **Targeted Logout Cleanup**: Logging out invalidates only that user's session token and cleans up only that user's workspace directory.

---

## 🛠️ Technology Stack

| Layer | Technologies |
|---|---|
| **Backend API** | FastAPI, Uvicorn, Python 3.10+ |
| **Web Crawling & Parsing** | Requests, BeautifulSoup4, Selenium WebDriver, PyTesseract |
| **Data Analytics & Reports** | Pandas, NumPy, OpenPyXL |
| **Database & ORM** | SQLAlchemy, PostgreSQL (Production / Render), SQLite (Local) |
| **Real-Time Communication** | WebSockets (`websockets`) |
| **Frontend Dashboard** | React 18 (SPA), Tailwind CSS, ApexCharts, Glassmorphism UI |

---

## 🚀 Getting Started

### Prerequisites
- **Python 3.10+** installed.
- **Google Chrome** installed (required if using Selenium browser driver mode).
- **Tesseract-OCR** installed (optional for automatic CAPTCHA OCR solving).

### Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/Uttama786/RESULT-ANALYSIS.git
   cd RESULT-ANALYSIS
   ```

2. **Install Python dependencies**:
   ```bash
   pip install -r backend/requirements.txt
   ```

3. **Configure Environment Variables** (Optional, `.env` file in `backend/`):
   ```env
   DATABASE_URL=sqlite:///app.db
   DATA_DIR=.
   SERVER_API_KEY=
   CLOUDINARY_URL=
   ```

4. **Run the Application Server**:
   ```bash
   cd backend
   python app.py
   ```
   Or using Uvicorn directly:
   ```bash
   uvicorn app:app --host 0.0.0.0 --port 8000 --reload
   ```

5. **Access the Dashboard**:
   Open your browser and navigate to `http://localhost:8000/`.

---

## 🔐 Default Credentials

| Role | Username | Default Password | Access Level |
|---|---|---|---|
| **System Admin** | `UttamBhise` | `#Uttama207` | Full System & User Management Access |
| **Demo Admin** | `admin` | `admin123` | Faculty / Administrator Access |
| **Demo Student** | `student` | `student123` | Student / Viewer Access |

---

## 🌐 Deployment on Render

1. **Create Web Service** on [Render.com](https://render.com).
2. **Connect Repository**: `Uttama786/RESULT-ANALYSIS`.
3. **Environment Settings**:
   - **Environment**: Python
   - **Root Directory**: `backend`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python app.py`
4. **Environment Variables**:
   - `DATABASE_URL`: Add Render PostgreSQL Connection String (`postgresql://...`).

---

## 🏛️ Institution & Credits

Developed for **Yenepoya Institute of Technology (YIT)**, Moodbidri, Mangaluru, Karnataka.

- **Author**: Uttam Bhise ([uttamabhise@gmail.com](mailto:uttamabhise@gmail.com))
- **Repository**: [github.com/Uttama786/RESULT-ANALYSIS](https://github.com/Uttama786/RESULT-ANALYSIS)
- **License**: MIT License
