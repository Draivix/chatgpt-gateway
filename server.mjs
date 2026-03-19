// ChatGPT Gateway Server
// HTTP API + WebSocket relay to Chrome extension
import { createServer } from "node:http";
import { WebSocketServer } from "ws";

const PORT = parseInt(process.env.PORT || "18790", 10);

// Pending HTTP requests waiting for extension response
const pending = new Map(); // requestId -> { resolve, reject, timer }

let extensionWs = null;

// ── WebSocket server (extension connects here) ────────────

const wss = new WebSocketServer({ noServer: true });

wss.on("connection", (ws) => {
  console.log("[gateway] Extension connected");
  extensionWs = ws;

  ws.on("message", (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      return;
    }

    if (msg.type === "response" && pending.has(msg.requestId)) {
      const { resolve, timer } = pending.get(msg.requestId);
      clearTimeout(timer);
      pending.delete(msg.requestId);
      resolve(msg);
    }
  });

  ws.on("close", () => {
    console.log("[gateway] Extension disconnected");
    if (extensionWs === ws) extensionWs = null;
  });
});

// ── HTTP server ────────────────────────────────────────────

const server = createServer(async (req, res) => {
  // CORS
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader(
    "Access-Control-Allow-Headers",
    "Content-Type, Authorization"
  );

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  // Health check
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        status: "ok",
        extensionConnected: !!extensionWs,
      })
    );
    return;
  }

  // OpenAI-compatible chat completions
  if (req.method === "POST" && req.url === "/v1/chat/completions") {
    let body = "";
    for await (const chunk of req) body += chunk;

    let payload;
    try {
      payload = JSON.parse(body);
    } catch {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Invalid JSON" }));
      return;
    }

    if (!extensionWs || extensionWs.readyState !== 1) {
      res.writeHead(503, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          error:
            "Extension not connected. Load the extension and open chatgpt.com",
        })
      );
      return;
    }

    const requestId = crypto.randomUUID();
    const timeoutMs = parseInt(payload.timeout || "180000", 10);

    const promise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(new Error("Timeout waiting for ChatGPT response"));
      }, timeoutMs);
      pending.set(requestId, { resolve, reject, timer });
    });

    // Send to extension
    extensionWs.send(
      JSON.stringify({
        type: "chat",
        requestId,
        model: payload.model || "auto",
        messages: payload.messages || [],
        newConversation: payload.new_conversation !== false,
      })
    );

    try {
      const result = await promise;

      if (!result.ok) {
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: result.error }));
        return;
      }

      // OpenAI-compatible response format
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          id: `chatcmpl-${requestId}`,
          object: "chat.completion",
          created: Math.floor(Date.now() / 1000),
          model: payload.model || "chatgpt-web",
          choices: [
            {
              index: 0,
              message: {
                role: "assistant",
                content: result.content,
              },
              finish_reason: "stop",
            },
          ],
          usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
        })
      );
    } catch (err) {
      res.writeHead(504, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: err.message }));
    }
    return;
  }

  // Fallback
  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Not found" }));
});

// WebSocket upgrade handler
server.on("upgrade", (req, socket, head) => {
  if (req.url === "/ws") {
    wss.handleUpgrade(req, socket, head, (ws) => {
      wss.emit("connection", ws, req);
    });
  } else {
    socket.destroy();
  }
});

server.listen(PORT, () => {
  console.log(`
  ChatGPT Gateway Server
  ──────────────────────
  HTTP API:    http://localhost:${PORT}/v1/chat/completions
  Health:      http://localhost:${PORT}/health
  WebSocket:   ws://localhost:${PORT}/ws

  Usage:
    curl -X POST http://localhost:${PORT}/v1/chat/completions \\
      -H "Content-Type: application/json" \\
      -d '{"model":"auto","messages":[{"role":"user","content":"Say hi"}]}'
  `);
});
