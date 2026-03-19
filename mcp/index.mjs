#!/usr/bin/env node

// MCP Server for ChatGPT Gateway
// Exposes your logged-in ChatGPT browser session as MCP tools.
// Connects to the local gateway server at localhost:18790.

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  CancelledNotificationSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const GATEWAY_URL =
  process.env.CHATGPT_GATEWAY_URL || "http://localhost:18790";
const DEFAULT_TIMEOUT = parseInt(
  process.env.CHATGPT_GATEWAY_TIMEOUT || "90000",
  10
);

// ── In-flight request tracking (for cancellation) ──────────

// Maps MCP request ID → AbortController
const inflight = new Map();

// ── Gateway HTTP client ────────────────────────────────────

async function gatewayHealth() {
  const res = await fetch(`${GATEWAY_URL}/health`, {
    signal: AbortSignal.timeout(5000),
  });
  if (!res.ok) throw new Error(`Gateway returned ${res.status}`);
  return res.json();
}

async function gatewayChatCompletions(body, signal) {
  const res = await fetch(`${GATEWAY_URL}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  const data = await res.json();
  if (!res.ok)
    throw new Error(data.error || `Gateway returned ${res.status}`);
  return data;
}

// ── Progress helper ────────────────────────────────────────

async function sendProgress(server, token, progress, total, message) {
  if (!token) return;
  try {
    await server.notification({
      method: "notifications/progress",
      params: { progressToken: token, progress, total, message },
    });
  } catch {
    // Progress notifications are best-effort
  }
}

// ── Tool definitions ───────────────────────────────────────

const tools = [
  {
    name: "chatgpt_status",
    description:
      "Check if the ChatGPT Gateway is running and the browser extension is connected. " +
      "Call this before sending messages to verify the gateway is available.",
    inputSchema: {
      type: "object",
      properties: {},
    },
    handler: async (_args, _server, _progressToken, _signal) => {
      try {
        const health = await gatewayHealth();
        const lines = [
          `Gateway: running`,
          `Extension connected: ${health.extensionConnected}`,
        ];
        if (!health.extensionConnected) {
          lines.push(
            "",
            "The browser extension is not connected.",
            "Make sure:",
            "  1. The extension is loaded in Chrome/Firefox",
            "  2. chatgpt.com is open in a tab",
            "  3. The page has been refreshed after loading the extension"
          );
        }
        return { text: lines.join("\n"), ok: health.extensionConnected };
      } catch (err) {
        return {
          text: [
            "Gateway server is not running.",
            "",
            "Start it with:",
            `  cd ${process.env.CHATGPT_GATEWAY_DIR || "chatgpt-gateway"} && npm start`,
          ].join("\n"),
          ok: false,
          isError: true,
        };
      }
    },
  },
  {
    name: "chatgpt_ask",
    description:
      "Send a message to ChatGPT through your logged-in browser session and get the response. " +
      "This tool automates a real browser tab, so it takes 10-120 seconds to complete. " +
      "Use chatgpt_status first to verify the gateway is available. " +
      "Each call starts a new ChatGPT conversation by default. " +
      "No API key is needed — uses your existing ChatGPT subscription. " +
      "Supports cancellation — the request will abort cleanly if cancelled.",
    inputSchema: {
      type: "object",
      properties: {
        message: {
          type: "string",
          description: "The message to send to ChatGPT",
        },
        model: {
          type: "string",
          description:
            'Model hint for ChatGPT model picker (e.g. "gpt-4o", "gpt-4o-mini"). Default: "auto"',
        },
        system_prompt: {
          type: "string",
          description:
            "Optional system-level instruction. Prepended to the user message as context.",
        },
        new_conversation: {
          type: "boolean",
          description:
            "Start a fresh conversation (default: true). Set to false to continue in the current ChatGPT conversation.",
        },
        timeout: {
          type: "number",
          description: `Max seconds to wait for ChatGPT response (default: ${DEFAULT_TIMEOUT / 1000}). Increase for complex prompts.`,
        },
      },
      required: ["message"],
    },
    handler: async (args, server, progressToken, signal) => {
      const timeoutMs = (args.timeout || DEFAULT_TIMEOUT / 1000) * 1000;

      // Step 1: Check gateway availability
      await sendProgress(
        server,
        progressToken,
        0,
        4,
        "Checking gateway availability..."
      );

      if (signal.aborted) throw new Error("Cancelled");

      let health;
      try {
        health = await gatewayHealth();
      } catch {
        return {
          text: "Gateway server is not running. Start it with: npm start (in the chatgpt-gateway directory)",
          isError: true,
        };
      }

      if (!health.extensionConnected) {
        return {
          text: "Browser extension is not connected. Make sure the extension is loaded and chatgpt.com is open.",
          isError: true,
        };
      }

      // Step 2: Prepare and send message
      await sendProgress(
        server,
        progressToken,
        1,
        4,
        "Typing message into ChatGPT..."
      );

      if (signal.aborted) throw new Error("Cancelled");

      const messages = [];
      if (args.system_prompt) {
        messages.push({ role: "system", content: args.system_prompt });
      }
      messages.push({ role: "user", content: args.message });

      // Step 3: Waiting for response (this is the long part)
      await sendProgress(
        server,
        progressToken,
        2,
        4,
        "Waiting for ChatGPT to respond..."
      );

      let result;
      try {
        result = await gatewayChatCompletions(
          {
            model: args.model || "auto",
            messages,
            new_conversation: args.new_conversation !== false,
            timeout: timeoutMs,
          },
          signal
        );
      } catch (err) {
        if (signal.aborted || err.name === "AbortError") {
          throw new Error("Cancelled");
        }
        return {
          text: `ChatGPT request failed: ${err.message}`,
          isError: true,
        };
      }

      // Step 4: Done
      await sendProgress(server, progressToken, 4, 4, "Response received");

      const content = result.choices?.[0]?.message?.content || "";
      return { text: content, model: result.model };
    },
  },
];

// ── MCP Server ─────────────────────────────────────────────

const server = new Server(
  { name: "chatgpt-gateway-mcp", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

// Handle cancellation notifications from client
server.setNotificationHandler(CancelledNotificationSchema, async (msg) => {
  const requestId = msg.params.requestId;
  const controller = inflight.get(requestId);
  if (controller) {
    console.error(
      `[mcp] Cancelling request ${requestId}: ${msg.params.reason || "client cancelled"}`
    );
    controller.abort();
    inflight.delete(requestId);
  }
});

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: tools.map((t) => ({
    name: t.name,
    description: t.description,
    inputSchema: t.inputSchema,
  })),
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const progressToken = request.params._meta?.progressToken;
  const requestId = request.id;

  const tool = tools.find((t) => t.name === name);
  if (!tool) {
    return {
      content: [{ type: "text", text: `Unknown tool: ${name}` }],
      isError: true,
    };
  }

  // Create AbortController for cancellation support
  const controller = new AbortController();
  inflight.set(requestId, controller);

  try {
    const result = await tool.handler(
      args || {},
      server,
      progressToken,
      controller.signal
    );
    return {
      content: [{ type: "text", text: result.text }],
      ...(result.isError ? { isError: true } : {}),
    };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (message === "Cancelled" || controller.signal.aborted) {
      return {
        content: [{ type: "text", text: "Request cancelled." }],
        isError: true,
      };
    }
    return {
      content: [{ type: "text", text: `Error: ${message}` }],
      isError: true,
    };
  } finally {
    inflight.delete(requestId);
  }
});

// ── Start ──────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[chatgpt-gateway-mcp] Server started (stdio transport)");
