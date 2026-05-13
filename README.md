# ZipBot — Production Telegram Zip/Unzip Bot

A modular, async Python Telegram bot for compressing and extracting files with:

- **Session isolation** per job (unique temp directory, no shared state)
- **AES-256 zip encryption** via pyzipper
- **Zip-bomb protection** with configurable decompression limits
- **Rate-safe admin logging** with coalescing and exponential backoff
- **MongoDB persistence** (users, jobs, limits, audit logs)
- **Local Telegram Bot API server** support for files beyond 50 MB
- **Railway-ready** Docker deployment

---

## Project structure

```
telegram-zip-bot/
├── bot/
│   ├── main.py                  # Entry point, handler registration
│   ├── config.py                # Env var loading, typed Config dataclass
│   ├── handlers/
│   │   ├── user_handlers.py     # /start /zip /unzip /cancel /status + file receipt
│   │   ├── admin_handlers.py    # /ban /unban /setlimit /userinfo /stats /jobs /canceljob
│   │   └── file_handlers.py     # Download → process → upload pipeline
│   ├── core/
│   │   ├── session.py           # JobSession: per-job temp directory lifecycle
│   │   └── queue.py             # JobQueue: async semaphore + per-user locks
│   ├── services/
│   │   ├── zip_service.py       # Chunked compression, optional AES-256
│   │   ├── unzip_service.py     # Safe extraction, encryption detection, typed errors
│   │   └── admin_logger.py      # Rate-safe admin Telegram sender
│   └── db/
│       ├── mongo.py             # Motor connection + index creation
│       └── repositories.py      # All MongoDB read/write operations
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Quick start (local development)

### 1. Clone and install

```bash
git clone <repo>
cd telegram-zip-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your values
```

### 3. Start the local Telegram Bot API server

The bot **requires** a self-hosted [Telegram Bot API server](https://github.com/tdlib/telegram-bot-api) for large-file support.

**Docker (recommended):**
```bash
docker run -d --name telegram-bot-api \
  -p 8081:8081 \
  -e TELEGRAM_API_ID=<your_api_id> \
  -e TELEGRAM_API_HASH=<your_api_hash> \
  aiogram/telegram-bot-api:latest \
  --api-id=<api_id> --api-hash=<api_hash> --local
```

Set `TELEGRAM_BOT_API_URL=http://localhost:8081` in your `.env`.

### 4. Start MongoDB

```bash
# Local via Docker:
docker run -d --name mongo -p 27017:27017 mongo:7
# Then set MONGO_URI=mongodb://localhost:27017 in .env
```

### 5. Run the bot

```bash
python -m bot.main
```

The startup sequence:
1. Validates all required env vars (exits immediately if any are missing).
2. Pings the local Bot API server — exits if unreachable.
3. Connects to MongoDB and creates indexes.
4. Starts the AdminLogger background sender.
5. Begins polling for updates.

---

## Deploying on Railway

### Services needed

Deploy two Railway services in the same project:

| Service | Docker image / source |
|---|---|
| **telegram-bot-api** | `aiogram/telegram-bot-api` or build from source |
| **zipbot** | This repo (Dockerfile at root) |

### Step-by-step

1. **Create a Railway project** and add a MongoDB plugin (or use MongoDB Atlas).
2. **Add the `telegram-bot-api` service:**
   - Use `aiogram/telegram-bot-api` as the Docker image.
   - Set env vars: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`.
   - Start command: `telegram-bot-api --api-id=$TELEGRAM_API_ID --api-hash=$TELEGRAM_API_HASH --local --http-port=8081`
   - This service does **not** need a public domain; the bot reaches it via Railway's internal network.
3. **Add the `zipbot` service:**
   - Point to this repository; Railway will use the Dockerfile.
   - Set all variables from `.env.example`.
   - Set `TELEGRAM_BOT_API_URL` to the internal Railway URL of the `telegram-bot-api` service:
     `http://telegram-bot-api.railway.internal:8081`
   - Set `MONGO_URI` to the Railway MongoDB connection string.
4. **Deploy both services.** Check the `zipbot` logs for the startup health-check confirmation.

### Railway resource notes

- The bot stores all in-flight files in `TEMP_DIR` (`/app/temp`). Railway ephemeral disk is limited per plan.
- Set `MAX_CONCURRENT_JOBS=2` on hobby plans to avoid disk and memory pressure.
- Set `MAX_FILE_SIZE_MB` to match your disk quota minus headroom.
- The local Bot API server also writes files to its own working directory — allocate separate disk for it or use the `--local` flag so it writes to the Railway volume.

---

## User commands

| Command | Description |
|---|---|
| `/start` / `/help` | Welcome and usage instructions |
| `/zip` | Reply to a file to compress it. Optionally: `/zip mypassword` |
| `/unzip` | Reply to a zip file to extract it |
| `/cancel` | Cancel your running job |
| `/status` | Show your current job status |

**Workflow:**
1. Send a file to the bot.
2. The bot stores the file reference and prompts you.
3. Reply with `/zip` or `/unzip` (or send the file with the command as caption).
4. For encrypted archives, the bot detects encryption and asks for the password.
5. The result files are sent back to you.

---

## Admin commands

All admin commands are restricted to `ADMIN_USER_IDS`.

| Command | Description |
|---|---|
| `/ban <user_id>` | Ban a user |
| `/unban <user_id>` | Unban a user |
| `/setlimit <user_id> <gb>` | Set daily GB processing limit |
| `/userinfo <user_id>` | Show user profile and recent jobs |
| `/stats` | Global bot statistics |
| `/jobs` | List active/pending jobs |
| `/canceljob <job_id_prefix>` | Force-cancel a running job |

---

## Operational limits and safety design

### Why streaming?

Loading a 2 GB file into Python memory on a Railway hobby instance (512 MB RAM) would immediately OOM-kill the process. All file I/O uses a fixed 256 KB chunk buffer. Compression and extraction write intermediate results to disk, not RAM.

### Why a job queue?

Without a concurrency limit, 10 simultaneous users uploading 500 MB files would saturate both CPU and disk simultaneously. The `JobQueue` semaphore caps heavy work at `MAX_CONCURRENT_JOBS`. Additional requests queue behind the semaphore rather than failing.

### Zip-bomb protection

Before extraction starts:
- The archive's declared uncompressed sizes are summed.
- If the total exceeds 50× the compressed input size, the job is rejected.
- During extraction, the running total is tracked in real time and the job is aborted if the limit is crossed mid-stream.
- Archives with more than 10,000 entries are rejected.

### Why session isolation?

Every job runs inside `./temp/{user_id}_{uuid4}/`. There is no global working directory. Two users processing a file named `archive.zip` simultaneously write to completely separate paths. The `try/finally` in `JobSession.__aexit__` ensures cleanup even if the job crashes.

### Admin logging rate safety

Telegram's Bot API enforces a rate limit of ~30 messages/second globally and 1 message/second per chat. The `AdminLogger` queues messages in an `asyncio.Queue` and sends them with a 1.2-second spacing. On `RetryAfter` responses it waits the specified duration plus 0.5 seconds. Persistent failures use exponential backoff up to 60 seconds, with a maximum of 5 retries before dropping the message and logging a warning.

---

## Risks and assumptions

| Risk | Mitigation |
|---|---|
| **Self-hosted Bot API server required for >50 MB files** | Clearly documented; startup health-check aborts if unreachable |
| **Railway disk is ephemeral and limited** | `DISK_FREE_HEADROOM_BYTES` (512 MB) checked before every job; configurable via env vars |
| **Railway memory limits** | Streaming-only I/O; no full-file buffering in RAM |
| **Telegram rate limits** | AdminLogger queue with spacing, backoff, and coalescing |
| **Plaintext password storage** | `ALLOW_PLAINTEXT_PASSWORD_LOGS=false` by default; documented risk in `.env.example`; passwords stored in MongoDB (encrypt at rest in production) |
| **Zip bombs** | Pre-scan + real-time limit enforced with configurable multiplier |
| **Path traversal in archives** | `_safe_member_name()` strips `..` and absolute paths |
| **Single-process Python GIL** | CPU-heavy compression runs in `asyncio.to_thread()` (separate OS thread), keeping the event loop responsive |
| **No webhook TLS termination** | Bot uses polling by default; switch to webhook + reverse proxy for production if Railway exposes a public HTTPS URL |
