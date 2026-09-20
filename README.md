# 🚗 Hot Wheels Hunt

Monitors **FirstCry.com** for Hot Wheels car availability, tracks stock changes
over time, checks delivery to your pincode, and sends alerts via **Email**,
**Telegram**, or **WhatsApp**.

---

## Features

| Feature       | Detail                                                  |
| ------------- | ------------------------------------------------------- |
| Scraping      | Playwright headless browser (handles JS-rendered pages) |
| Detection     | New products, Restocks, Out-of-stock, Price drops (≥5%) |
| Pincode check | Browser-automated delivery check per changed product    |
| Scheduler     | APScheduler — runs every N minutes (default: 10)        |
| Notifications | Email (SMTP), Telegram Bot, WhatsApp (Twilio)           |
| Dashboard     | Vanilla-JS SPA — product grid, stats bar, event log     |
| Database      | SQLite via SQLAlchemy                                   |
| Deployment    | Local (uvicorn) or Docker                               |

---

## Architecture

```mermaid
flowchart TB
    subgraph External["🌐 External"]
        FC[FirstCry.com]
        SMTP[Gmail SMTP]
        TG[Telegram API]
        TW[Twilio WhatsApp API]
    end

    subgraph Container["🐳 Docker Container / Local Process"]
        subgraph Backend["FastAPI Backend (backend/)"]
            SCH["scheduler.py<br/>APScheduler — run_check() every N min"]
            SCR["scraper.py<br/>Playwright / JSON API / BeautifulSoup"]
            PIN["pincode_checker.py<br/>Delivery check automation"]
            NOTIF["notifier.py<br/>Email · Telegram · WhatsApp"]
            DB[("database.py<br/>SQLite (products, stock_events, app_settings)")]
            API["routers/*<br/>products · settings · events"]
            MAIN["main.py<br/>FastAPI app + lifespan + static files"]
        end
        FE["Frontend SPA (frontend/)<br/>index.html · app.js · style.css"]
    end

    User[("👤 User Browser")]

    MAIN -->|starts on boot| SCH
    SCH -->|1 scrape| SCR
    SCR -->|scrape| FC
    SCR -->|scraped products| SCH
    SCH -->|2 diff vs DB| DB
    SCH -->|3 check delivery for changed items| PIN
    PIN -->|simulate pincode entry| FC
    SCH -->|4 persist events/products| DB
    SCH -->|5 dispatch alerts| NOTIF
    NOTIF --> SMTP
    NOTIF --> TG
    NOTIF --> TW

    User <-->|HTTP GET /| MAIN
    MAIN -->|serves| FE
    FE -->|fetch /api/*| API
    API -->|read/write| DB
    API -->|POST /api/check triggers| SCH
```

### Check Pipeline Workflow

```mermaid
sequenceDiagram
    autonumber
    participant S as Scheduler (scheduler.py)
    participant SC as Scraper
    participant FC as FirstCry.com
    participant DB as SQLite DB
    participant P as Pincode Checker
    participant N as Notifier
    participant U as User (Email/Telegram/WhatsApp)

    S->>SC: scrape_hot_wheels()
    par Concurrent page fetches
        SC->>FC: JSON paging API (fast path)
    and
        SC->>FC: Playwright fallback (JS render)
    and
        SC->>FC: BeautifulSoup last resort
    end
    FC-->>SC: product pages (streamed as they land)

    loop for each scraped page batch
        SC-->>S: batch of ScrapedProduct
        S->>DB: lookup existing product_id
        alt product not in DB
            S->>DB: insert Product + NEW event
        else exists & was OOS, now available
            S->>DB: update Product + RESTOCK event
        else exists & was available, now OOS
            S->>DB: update Product + OOS event
        else price dropped ≥5%
            S->>DB: update Product + PRICE_DROP event
        end

        opt NEW or RESTOCK event
            S->>P: batch_check_pincode(changed products)
            P->>FC: simulate pincode entry per product
            FC-->>P: deliverable true/false
            P-->>S: delivery results
        end

        S->>N: dispatch_notification(event, delivery info)
        N->>U: Email / Telegram / WhatsApp alert
    end

    S->>DB: purge delisted products (ids no longer scraped)
    S-->>S: store summary in live feed (last 30 runs)
```

### Event Detection Rules

```mermaid
flowchart LR
    A[Scraped Product] --> B{Exists in DB?}
    B -- No --> NEW["🆕 NEW event"]
    B -- Yes --> C{was OOS, now available?}
    C -- Yes --> RESTOCK["🔄 RESTOCK event"]
    C -- No --> D{was available, now OOS?}
    D -- Yes --> OOS["❌ OOS event"]
    D -- No --> E{price dropped ≥5%?}
    E -- Yes --> DROP["💸 PRICE_DROP event"]
    E -- No --> NONE[No event]

    NEW --> F{Notifications enabled?}
    RESTOCK --> F
    DROP --> F
    F -- Yes --> G[Check pincode delivery]
    G --> H[Send notification]
```

---

## Quick Start (Local)

### 1. Prerequisites

- Python 3.11+
- pip

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your values (pincode, notification keys, etc.)
```

### 4. Run

```bash
uvicorn backend.main:app --reload
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

---

## Docker

```bash
# Build & start
docker compose up -d --build

# View logs
docker compose logs -f

# Stop
docker compose down
```

The SQLite database is persisted in `./hotwheels.db` on the host.

---

## Environment Variables

| Variable                 | Default          | Description                                   |
| ------------------------ | ---------------- | --------------------------------------------- |
| `DEFAULT_PINCODE`        | `400001`         | Your 6-digit delivery pincode                 |
| `CHECK_INTERVAL_MINUTES` | `10`             | Scrape frequency                              |
| `MAX_PAGES`              | `5`              | Max search result pages per run (0 = all)     |
| `FIRSTCRY_SEARCH_URL`    | FirstCry search  | URL to scrape (can change to a category page) |
| `EMAIL_ENABLED`          | `false`          | Enable email alerts                           |
| `SMTP_HOST`              | `smtp.gmail.com` | SMTP server                                   |
| `SMTP_PORT`              | `587`            | SMTP port                                     |
| `SMTP_USER`              | —                | Gmail address                                 |
| `SMTP_PASSWORD`          | —                | Gmail App Password                            |
| `EMAIL_TO`               | —                | Recipient address                             |
| `TELEGRAM_ENABLED`       | `false`          | Enable Telegram alerts                        |
| `TELEGRAM_BOT_TOKEN`     | —                | `@BotFather` token                            |
| `TELEGRAM_CHAT_ID`       | —                | Your Telegram user/chat ID                    |
| `WHATSAPP_ENABLED`       | `false`          | Enable WhatsApp via Twilio                    |
| `TWILIO_ACCOUNT_SID`     | —                | Twilio Account SID                            |
| `TWILIO_AUTH_TOKEN`      | —                | Twilio Auth Token                             |
| `WHATSAPP_TO`            | —                | `whatsapp:+91XXXXXXXXXX`                      |

---

## Notification Setup

### Email (Gmail)

1. Enable **2-Step Verification** on your Google account.
2. Go to **Google Account → Security → App Passwords**.
3. Generate an App Password for "Mail".
4. Set `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` in `.env`.
5. Set `EMAIL_ENABLED=true`.

### Telegram Bot

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow prompts → copy the **token**.
3. Start a chat with your bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   to get your **chat_id**.
4. Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` in `.env`.
5. Set `TELEGRAM_ENABLED=true`.

### WhatsApp (Twilio)

1. Create a [Twilio](https://twilio.com) account.
2. Enable **WhatsApp Sandbox** in the Twilio Console.
3. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `WHATSAPP_TO`.
4. Set `WHATSAPP_ENABLED=true`.

---

## API Reference

| Method | Endpoint                     | Description                 |
| ------ | ---------------------------- | --------------------------- |
| `GET`  | `/api/products`              | List all tracked products   |
| `GET`  | `/api/products/{id}/history` | Stock history for a product |
| `GET`  | `/api/products/series/list`  | Available series names      |
| `POST` | `/api/check`                 | Trigger an immediate check  |
| `GET`  | `/api/stats`                 | Dashboard summary stats     |
| `GET`  | `/api/settings`              | Current settings            |
| `PUT`  | `/api/settings`              | Update settings             |
| `GET`  | `/api/events`                | Recent stock events         |

Interactive API docs: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## Example Notification

```
🔄 Hot Wheels Alert — Back In Stock!

Product : Hot Wheels Car Culture Premium Assorted
Price   : ₹599 (MRP ₹799 — 25% off)
Stock   : In Stock ✅
Delivery: ✅ Deliverable to 400001
Series  : Car Culture
Link    : https://www.firstcry.com/...

Checked at 2025-01-15 10:30 UTC
```

---

## Project Structure

```
hotwheels-hunt/
├── backend/
│   ├── main.py            # FastAPI app + lifespan
│   ├── config.py          # Pydantic settings from .env
│   ├── database.py        # SQLAlchemy models & DB init
│   ├── models.py          # Pydantic schemas
│   ├── scraper.py         # Playwright scraper
│   ├── pincode_checker.py # Delivery check automation
│   ├── notifier.py        # Email / Telegram / WhatsApp
│   ├── scheduler.py       # APScheduler pipeline
│   └── routers/
│       ├── products.py
│       ├── settings.py
│       └── events.py
├── frontend/
│   ├── index.html         # Dashboard SPA
│   ├── style.css
│   └── app.js
├── .env.example
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## Notes & Limitations

- **Anti-scraping**: The scraper uses stealth headers and random delays.
  If FirstCry changes their HTML structure, update selectors in `scraper.py`.
- **Pincode check**: Performed only for _changed_ products (new/restock/price-drop)
  to minimise browser sessions. Results are cached in-memory for the session.
- **Rate limiting**: Default interval is 10 min; avoid setting it below 5 min.
- **Playwright + Docker**: The Dockerfile installs Chromium system deps automatically.
