// ChatGPT Gateway — Content script
// Injected into chatgpt.com, handles DOM automation
(function () {
  "use strict";

  const POLL_INTERVAL = 1500;
  const MAX_WAIT = 180_000; // 3 minutes

  // ── Helpers ──────────────────────────────────────────────

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function findInput() {
    const selectors = [
      "#prompt-textarea",
      'div[contenteditable="true"][id="prompt-textarea"]',
      "textarea",
      '[contenteditable="true"]',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return el;
    }
    return null;
  }

  function findSendButton() {
    const selectors = [
      '[data-testid="send-button"]',
      "#composer-submit-button",
      'button[aria-label="Send prompt"]',
      'button[aria-label*="Send"]',
    ];
    for (const sel of selectors) {
      const btn = document.querySelector(sel);
      if (btn && !btn.disabled) return btn;
    }
    return null;
  }

  function countAssistantMessages() {
    return document.querySelectorAll(
      '[data-message-author-role="assistant"]'
    ).length;
  }

  function getLatestAssistantText() {
    const els = document.querySelectorAll(
      '[data-message-author-role="assistant"]'
    );
    if (els.length === 0) return "";
    const last = els[els.length - 1];
    return (last.textContent || "")
      .replace(/[\u200B-\u200D\uFEFF]/g, "")
      .trim();
  }

  function isStreaming() {
    return !!document.querySelector(
      'button[aria-label="Stop streaming"], [data-testid="stop-button"], button[aria-label*="Stop"]'
    );
  }

  // ── Actions ──────────────────────────────────────────────

  async function startNewChat() {
    // Try the "New chat" button/link
    const selectors = [
      'a[href="/"]',
      '[data-testid="create-new-chat-button"]',
      'nav a[href="/"]',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) {
        el.click();
        await sleep(1500);
        return;
      }
    }
    // Fallback: navigate
    window.location.href = "https://chatgpt.com/";
    await sleep(2500);
  }

  async function selectModel(modelSlug) {
    if (!modelSlug) return;

    // Try clicking the model picker
    const pickerSelectors = [
      '[data-testid="model-switcher"]',
      'button[aria-haspopup="listbox"]',
      'button[aria-haspopup="menu"]',
    ];

    let picker = null;
    for (const sel of pickerSelectors) {
      picker = document.querySelector(sel);
      if (picker) break;
    }

    if (!picker) return; // No model picker found, proceed with default

    picker.click();
    await sleep(800);

    // Find model option
    const options = document.querySelectorAll(
      '[role="option"], [role="menuitemradio"], [role="menuitem"]'
    );
    const slug = modelSlug.toLowerCase();
    for (const opt of options) {
      const text = (opt.textContent || "").toLowerCase();
      if (text.includes(slug) || text.includes(slug.replace(/-/g, " "))) {
        opt.click();
        await sleep(500);
        return;
      }
    }
    // Close picker if model not found
    document.body.click();
    await sleep(300);
  }

  async function typeMessage(text) {
    const input = findInput();
    if (!input) throw new Error("Cannot find ChatGPT input field");

    input.focus();
    await sleep(200);

    // Select all existing content and delete it
    document.execCommand("selectAll");
    document.execCommand("delete");
    await sleep(100);

    // Insert text — execCommand works with both textarea and contenteditable
    // For long texts, chunk it to avoid issues
    if (text.length > 4000) {
      // For very long text, set directly and dispatch events
      if (input.contentEditable === "true") {
        // ProseMirror/contenteditable
        const p = document.createElement("p");
        p.textContent = text;
        input.innerHTML = "";
        input.appendChild(p);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      } else {
        input.value = text;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
    } else {
      document.execCommand("insertText", false, text);
    }

    await sleep(400);

    // Verify text was entered
    const currentText =
      input.contentEditable === "true"
        ? (input.textContent || "").trim()
        : (input.value || "").trim();

    if (!currentText) {
      // Retry with direct assignment
      if (input.contentEditable === "true") {
        input.textContent = text;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      } else {
        input.value = text;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      await sleep(400);
    }
  }

  async function clickSend() {
    // Wait a moment for the send button to become enabled
    for (let i = 0; i < 10; i++) {
      const btn = findSendButton();
      if (btn && !btn.disabled) {
        btn.click();
        return;
      }
      await sleep(300);
    }
    throw new Error("Send button not found or disabled");
  }

  async function waitForNewResponse(beforeCount) {
    let lastText = "";
    let stableCount = 0;

    for (let elapsed = 0; elapsed < MAX_WAIT; elapsed += POLL_INTERVAL) {
      await sleep(POLL_INTERVAL);

      const currentCount = countAssistantMessages();
      if (currentCount <= beforeCount) continue; // No new response yet

      const text = getLatestAssistantText();
      const streaming = isStreaming();

      if (text && text !== lastText) {
        lastText = text;
        stableCount = 0;
      } else if (text) {
        stableCount++;
        if (!streaming && stableCount >= 2) {
          return text;
        }
      }
    }

    if (lastText) return lastText;
    throw new Error("Timeout: no response from ChatGPT");
  }

  // ── Message handler ──────────────────────────────────────

  async function handleChatRequest(msg) {
    try {
      const beforeCount = countAssistantMessages();

      // Start new conversation if requested
      if (msg.newConversation !== false) {
        await startNewChat();
        await selectModel(msg.model);
      }

      await typeMessage(msg.userMessage);
      await clickSend();

      const response = await waitForNewResponse(beforeCount);

      return { ok: true, content: response };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  }

  // ── Listen for messages from background script ───────────

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "ping") {
      sendResponse({ ok: true, url: window.location.href });
      return;
    }

    if (msg.type === "chat") {
      handleChatRequest(msg).then(sendResponse);
      return true; // Will respond asynchronously
    }
  });

  console.log("[ChatGPT Gateway] Content script loaded on", window.location.href);
})();
