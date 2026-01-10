# Twitch Chat Helper

A minimal FastAPI + WebSocket service that streams a Twitch chat into a shared spotlight-friendly viewer. Every connected browser receives the same updates in real time, and any viewer can enlarge a message to share it with an audience, stage crew, or caster desk.

## Features
- Live ingestion of a Twitch channel via the IRC gateway.
- WebSocket fan-out so every connected client stays in sync.
- Click-to-highlight overlay with a dismiss button on the top-right corner.
- Lightweight fake message generator (`FAKE_CHAT_MODE=1`) for local demos when Twitch credentials are unavailable.

## Setup
1. **Python environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Run the server**
   ```bash
   uvicorn app.main:app --reload
   ```

3. **Open the UI**
   Visit http://localhost:8000/ and keep the page open. Multiple viewers can connect; everyone receives identical updates.

## How It Works
- A background task either connects to Twitch IRC (`irc.chat.twitch.tv:6697`) or emits fake messages. Parsed chat lines are transformed into JSON payloads stored in a short rolling history.
- Each WebSocket connection immediately receives the history, then every new message broadcast.
- The frontend (`app/static`) consumes the socket, renders the feed, and handles the focus overlay UI.

## Customization Ideas
- Increase `HISTORY_LIMIT` in `app/main.py` if you need more scrollback.
- Extend the frontend to flag messages, add keyboard shortcuts, or drive on-stream graphics.
- Swap the fake generator for a REST endpoint if you want producers to push curated lines manually.
