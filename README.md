# ChatGPT Gateway

Browser extension + local server that turns your logged-in ChatGPT session into an OpenAI-compatible API.

No API keys needed — uses your existing ChatGPT subscription through browser automation.

![ChatGPT Gateway Icon](icons/icon128.png)

## How it works

```
Your app / curl / any OpenAI client
        ↓ HTTP
Gateway server (localhost:18790)
        ↓ WebSocket
Browser extension
        ↓ DOM automation
ChatGPT (your logged-in session)
```

## Setup

### 1. Install the extension

**Chrome:**
1. Go to `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select this repo's root directory

**Firefox:**
1. Go to `about:debugging#/runtime/this-firefox`
2. Click **Load Temporary Add-on** → select `manifest.firefox.json`

### 2. Start the gateway server

```bash
npm install
npm start
```

### 3. Open ChatGPT

Navigate to [chatgpt.com](https://chatgpt.com) and make sure you're logged in. The extension popup will show green status when everything is connected.

## Usage

### curl

```bash
curl -X POST http://localhost:18790/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello!"}]}'
```

### Python

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:18790/v1", api_key="unused")
response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### Node.js

```javascript
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://localhost:18790/v1", apiKey: "unused" });
const response = await client.chat.completions.create({
  model: "auto",
  messages: [{ role: "user", content: "Hello!" }],
});
console.log(response.choices[0].message.content);
```

## API

### `POST /v1/chat/completions`

OpenAI-compatible chat completions endpoint.

| Field | Type | Description |
|---|---|---|
| `messages` | array | Array of `{role, content}` message objects |
| `model` | string | Model hint for ChatGPT's model picker (default: `"auto"`) |
| `new_conversation` | boolean | Start a new chat (default: `true`) |

### `GET /health`

Returns gateway status and extension connection state.

## Project structure

```
├── manifest.json           # Chrome extension manifest (MV3)
├── manifest.firefox.json   # Firefox extension manifest (MV3)
├── background.js           # Service worker — WebSocket bridge to gateway
├── content.js              # Content script — ChatGPT DOM automation
├── popup.html/js           # Extension popup — connection status
├── server.mjs              # Gateway HTTP + WebSocket server
├── icons/                  # Extension icons
└── package.json            # Server dependencies
```

## Requirements

- Node.js 18+
- Chrome or Firefox
- Active ChatGPT session (free or Plus)

## License

MIT
