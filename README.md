# Modvera SCADA: AI-Powered Industrial Monitoring

Modvera SCADA is a comprehensive, high-performance industrial monitoring and data logging system. It features a dual-architecture with a persistent background service for Modbus communication and a modern, high-performance GUI for real-time visualization and control.

## 🚀 Key Features

- **Continuous Background Logging**: A dedicated service polls Modbus registers even when the GUI is closed.
- **AI-Driven Analytics**: Integrated situation analysis powered by **Claude-3 Sonnet (Anthropic)** for intelligent reporting.
- **Robust Multi-Profile Polling**: Supports concurrent polling of Coils, Discrete Inputs, Holding Registers, and Input Registers.
- **Atomic Persistence**: Uses a journaling SQLite database and atomic JSON writes to ensure zero data corruption during power failures.
- **Smart Alarming**: Real-time threshold monitoring with persistent log historical tracking.
- **Health Watchdog**: Built-in self-monitoring system with startup and runtime health telemetries.

## 🧠 AI & Technologies

This project leverages cutting-edge technology for both development and runtime analysis:
- **Claude-3 Sonnet AI**: Integrated as a real-time SCADA analyst to provide operational insights.
- **Python-Based Core**: Built with `pymodbus` for reliability and `CustomTkinter` for a premium UI experience.
- **Antigravity AI Integration**: The entire stabilization and architectural hardening phase was orchestrated by **Antigravity AI (Google DeepMind)**.

## 🛠️ Setup & Installation

### 1. Requirements
Ensure you have Python 3.10+ installed. Install the dependencies:
```bash
pip install -r requirements.txt
```

### 2. Configuration
Create a `.env` file from the provided template:
```bash
cp .env.example .env
```
Fill in the following details in `.env`:
- `ANTHROPIC_API_KEY`: Your API key for Claude analytics.
- `SENDER_EMAIL` & `SENDER_PASS`: For automated alarm/report emails.

### 3. Running the App
Start the main GUI:
```bash
python main.py
```
*The background service will be automatically initiated by the GUI if it is not already running.*

## 📸 Interface Preview

![Dashboard Screenshot](https://github.com/user-attachments/assets/your-screenshot-id-here)
*(Replace with your actual uploaded GitHub asset link)*

## 🏷️ Topics
`ai`, `scada`, `machine-learning`, `deep-learning`, `modbus`, `automation`, `industrial-iot`, `python`, `monitoring`, `system-monitoring`

---

