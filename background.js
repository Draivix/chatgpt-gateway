// ChatGPT Gateway — Background service worker
// Connects to local gateway server via WebSocket
"use strict";

const GATEWAY_URL = "ws://localhost:18790/ws";
let ws = null;
let reconnectTimer = null;

function log(...args) {
  console.log("[Gateway BG]", ...args);
}

function connect() {
  if (ws && ws.readyState === WebSocket.OPEN) return;

  try {
    ws = new WebSocket(GATEWAY_URL);
  } catch (err) {
    log("WebSocket constructor failed:", err.message);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    log("Connected to gateway server");
    if (reconnectTimer) {
      clearInterval(reconnectTimer);
      reconnectTimer = null;
    }
  };

  ws.onmessage = async (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      log("Invalid JSON from server");
      return;
    }

    if (msg.type === "chat") {
      await handleChatMessage(msg);
    }
  };

  ws.onclose = () => {
    log("Disconnected from gateway server");
    ws = null;
    scheduleReconnect();
  };

  ws.onerror = () => {
    // onclose will fire after this
    ws?.close();
  };
}

function scheduleReconnect() {
  if (!reconnectTimer) {
    reconnectTimer = setInterval(connect, 5000);
  }
}

function sendToServer(data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(data));
  }
}

async function handleChatMessage(msg) {
  // Find a ChatGPT tab
  const tabs = await chrome.tabs.query({ url: ["https://chatgpt.com/*", "https://chat.openai.com/*"] });

  if (tabs.length === 0) {
    sendToServer({
      type: "response",
      requestId: msg.requestId,
      ok: false,
      error: "No ChatGPT tab open. Please navigate to chatgpt.com",
    });
    return;
  }

  // Extract the last user message
  const messages = msg.messages || [];
  const lastUser = [...messages].reverse().find((m) => m.role === "user");
  const userMessage = lastUser?.content || "";

  if (!userMessage) {
    sendToServer({
      type: "response",
      requestId: msg.requestId,
      ok: false,
      error: "No user message in request",
    });
    return;
  }

  // Try each tab until one responds (handles multiple ChatGPT tabs)
  const errors = [];
  for (const tab of tabs) {
    try {
      const response = await chrome.tabs.sendMessage(tab.id, {
        type: "chat",
        model: msg.model,
        userMessage,
        newConversation: msg.newConversation !== false,
      });

      sendToServer({
        type: "response",
        requestId: msg.requestId,
        ok: response.ok,
        content: response.content || "",
        error: response.error || "",
      });
      return; // Success — stop trying other tabs
    } catch (err) {
      errors.push(`Tab ${tab.id}: ${err.message}`);
    }
  }

  // All tabs failed
  sendToServer({
    type: "response",
    requestId: msg.requestId,
    ok: false,
    error: `No ChatGPT tab responded (${tabs.length} tab(s) tried). Refresh chatgpt.com.\n${errors.join("\n")}`,
  });
}

// Connect on startup
connect();

// Reconnect on service worker events
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);

// Keep service worker alive with periodic alarm
chrome.alarms.create("keepAlive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "keepAlive") {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      connect();
    }
  }
});

log("Service worker started");
