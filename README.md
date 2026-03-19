# ChatGPT Gateway

Use ChatGPT from any code, script, or tool — without an API key.

This is a browser extension paired with a tiny local server that exposes your **logged-in ChatGPT session** as a standard OpenAI-compatible API at `localhost:18790`. Any application that speaks the OpenAI chat completions format can use it out of the box — just swap the base URL.

**Why?** OpenAI API access requires a separate paid plan and API keys. If you already have a ChatGPT subscription (Free, Plus, or Team), this tool lets you use it programmatically. It automates the ChatGPT web interface through your browser — exactly like you would manually, but from code.

![ChatGPT Gateway Icon](icons/icon128.png)

## How it works

```
Your app / curl / any OpenAI-compatible client
        ↓ HTTP POST (localhost:18790)
Local gateway server (Node.js)
        ↓ WebSocket
Browser extension (background worker)
        ↓ chrome.tabs message
Content script on chatgpt.com
        ↓ DOM automation (type → send → read response)
ChatGPT (your logged-in browser session)
```

The extension's content script types your message into ChatGPT's input field, clicks send, waits for the response to finish streaming, extracts the text, and returns it through the chain. The gateway server wraps the result in an OpenAI-compatible JSON response.

## Can I keep using my browser?

**Yes.** The extension only interacts with the ChatGPT tab. All other tabs work normally. You can browse, work, watch videos — whatever you want.

The only rule: **don't manually type in the ChatGPT tab while a request is in progress.** The content script is actively using that tab's input field and reading the response DOM. Between requests, you can use ChatGPT manually as usual.

You do **not** need a separate browser profile or window. Your everyday Chrome or Firefox works fine.

## Setup

### 1. Install the extension

**Chrome:**
1. Go to `chrome://extensions`
2. Enable **Developer mode** (toggle in top-right)
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

Navigate to [chatgpt.com](https://chatgpt.com) and make sure you're logged in. Click the extension icon to verify — both dots should be green.

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

| Field | Type | Default | Description |
|---|---|---|---|
| `messages` | array | required | Array of `{role, content}` message objects |
| `model` | string | `"auto"` | Model hint — attempts to select via ChatGPT's model picker |
| `new_conversation` | boolean | `true` | Start a fresh chat for each request |
| `timeout` | number | `180000` | Max wait time in ms for ChatGPT to respond |

Response format matches the [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat/create).

### `GET /health`

```json
{"status": "ok", "extensionConnected": true}
```

## MCP Server

The `mcp/` directory contains a [Model Context Protocol](https://modelcontextprotocol.io) server that wraps the gateway. This lets AI tools like Claude Code use ChatGPT directly as a tool.

### Install

```bash
cd mcp && npm install
```

### Configure (Claude Code)

Add to your MCP settings (`~/.claude/settings.json` or project `.claude/settings.json`):

```json
{
  "mcpServers": {
    "chatgpt": {
      "command": "node",
      "args": ["/path/to/chatgpt-gateway/mcp/index.mjs"]
    }
  }
}
```

### Tools

| Tool | Description |
|---|---|
| `chatgpt_status` | Check if the gateway is running and extension is connected |
| `chatgpt_ask` | Send a message to ChatGPT and get the response (10-120s, sends progress notifications) |

The `chatgpt_ask` tool supports progress notifications per the MCP spec — clients will see status updates like "Typing message...", "Waiting for response..." while ChatGPT processes the request.

## Project structure

```
├── manifest.json           # Chrome extension manifest (MV3)
├── manifest.firefox.json   # Firefox extension manifest (MV3)
├── background.js           # Service worker — WebSocket bridge to gateway
├── content.js              # Content script — ChatGPT DOM automation
├── popup.html / popup.js   # Extension popup — connection status UI
├── server.mjs              # Gateway HTTP + WebSocket server
├── icons/                  # Extension icons (16/32/48/128px)
├── package.json            # Server dependencies
└── mcp/                    # MCP server
    ├── index.mjs           # MCP stdio server
    └── package.json        # MCP dependencies
```

## Limitations

- **One request at a time.** The extension automates a single ChatGPT tab, so requests are serialized. Concurrent requests will queue on the server side.
- **No streaming.** Responses are returned after ChatGPT finishes generating. Streaming support may come later.
- **DOM-dependent.** If ChatGPT significantly changes its UI, the content script selectors may need updating.
- **Session-bound.** If your ChatGPT session expires, refresh the tab or re-login.

## Requirements

- Node.js 18+
- Chrome or Firefox
- Active ChatGPT session (Free, Plus, or Team)

## License

MIT
