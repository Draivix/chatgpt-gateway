#!/usr/bin/env node

// MCP Server for ChatGPT Gateway
// Exposes your logged-in ChatGPT browser session as MCP tools.
// Connects to the local gateway server at localhost:18790.

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const GATEWAY_URL =
  process.env.CHATGPT_GATEWAY_URL || "http://localhost:18790";

// ── Gateway HTTP client ────────────────────────────────────

async function gatewayHealth() {
  const res = await fetch(`${GATEWAY_URL}/health`);
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
  if (!res.ok) throw new Error(data.error || `Gateway returned ${res.status}`);
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
    handler: async (_args, _server, _progressToken) => {
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
      "No API key is needed — uses your existing ChatGPT subscription.",
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
      },
      required: ["message"],
    },
    handler: async (args, server, progressToken) => {
      // Step 1: Check gateway availability
      await sendProgress(
        server,
        progressToken,
        0,
        4,
        "Checking gateway availability..."
      );

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
        "Waiting for ChatGPT to respond (this may take a while)..."
      );

      let result;
      try {
        result = await gatewayChatCompletions({
          model: args.model || "auto",
          messages,
          new_conversation: args.new_conversation !== false,
          timeout: 180000,
        });
      } catch (err) {
        return {
          text: `ChatGPT request failed: ${err.message}`,
          isError: true,
        };
      }

      // Step 4: Done
      await sendProgress(
        server,
        progressToken,
        4,
        4,
        "Response received"
      );

      const content = result.choices?.[0]?.message?.content || "";
      return {
        text: content,
        model: result.model,
      };
    },
  },
];

// ── MCP Server ─────────────────────────────────────────────

const server = new Server(
  { name: "chatgpt-gateway-mcp", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

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

  const tool = tools.find((t) => t.name === name);
  if (!tool) {
    return {
      content: [{ type: "text", text: `Unknown tool: ${name}` }],
      isError: true,
    };
  }

  try {
    const result = await tool.handler(args || {}, server, progressToken);
    return {
      content: [{ type: "text", text: result.text }],
      ...(result.isError ? { isError: true } : {}),
    };
  } catch (err) {
    return {
      content: [
        {
          type: "text",
          text: `Error: ${err instanceof Error ? err.message : String(err)}`,
        },
      ],
      isError: true,
    };
  }
});

// ── Start ──────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[chatgpt-gateway-mcp] Server started (stdio transport)");
