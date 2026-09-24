"use strict";

// Keep the native host name in sync with install_native_host.py.
const NATIVE_HOST = "com.apm.website_tracker";
let nativePort = null;
let reconnectDelayMs = 1000;
let reconnectTimer = null;
let refreshGeneration = 0;
let lastSentDomain;

function scheduleReconnect() {
  if (reconnectTimer !== null) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectNativeHost();
  }, reconnectDelayMs);
  reconnectDelayMs = Math.min(reconnectDelayMs * 2, 30000);
}

function connectNativeHost() {
  if (nativePort !== null) return;

  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST);
  } catch (_error) {
    scheduleReconnect();
    return;
  }

  nativePort.onMessage.addListener((message) => {
    if (message && message.type === "ready") {
      reconnectDelayMs = 1000;
      // Reconcile state after installation, service-worker restarts, or a
      // native-host restart so the CSV starts from the browser's live tab.
      refreshActiveTab(true);
    }
  });

  nativePort.onDisconnect.addListener(() => {
    nativePort = null;
    lastSentDomain = undefined;
    scheduleReconnect();
  });
}

function domainFromTab(tab) {
  if (!tab || typeof tab.url !== "string") return null;
  try {
    const parsed = new URL(tab.url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return parsed.hostname.toLowerCase().replace(/\.$/, "") || null;
  } catch (_error) {
    // New tabs, browser-internal pages, and malformed URLs are not websites.
    return null;
  }
}

function sendDomain(domain, force = false) {
  if (nativePort === null || (!force && domain === lastSentDomain)) return;
  lastSentDomain = domain;
  nativePort.postMessage({
    type: "site",
    domain,
    observed_at: Date.now() / 1000,
  });
}

function refreshActiveTab(force = false) {
  const generation = ++refreshGeneration;
  chrome.windows.getLastFocused({ populate: true }, (window) => {
    if (generation !== refreshGeneration) return;
    if (chrome.runtime.lastError || !window || !window.focused) {
      sendDomain(null, force);
      return;
    }

    const activeTab = (window.tabs || []).find((tab) => tab.active);
    sendDomain(domainFromTab(activeTab), force);
  });
}

function handleWindowFocusChanged(windowId) {
  // WINDOW_ID_NONE means focus moved outside Brave. Invalidate pending
  // asynchronous lookups so an old tab result cannot reopen a session.
  const generation = ++refreshGeneration;
  if (windowId === chrome.windows.WINDOW_ID_NONE) {
    sendDomain(null);
    return;
  }

  chrome.windows.get(windowId, { populate: true }, (window) => {
    if (generation !== refreshGeneration) return;
    if (chrome.runtime.lastError || !window || !window.focused) {
      sendDomain(null);
      return;
    }
    const activeTab = (window.tabs || []).find((tab) => tab.active);
    sendDomain(domainFromTab(activeTab));
  });
}

chrome.tabs.onActivated.addListener(() => refreshActiveTab());
chrome.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
  if (tab.active || changeInfo.url) refreshActiveTab();
});
chrome.tabs.onRemoved.addListener(() => refreshActiveTab());
chrome.tabs.onReplaced.addListener(() => refreshActiveTab());
chrome.windows.onFocusChanged.addListener(handleWindowFocusChanged);

// These events catch client-side history changes and full navigations even
// when a tab's URL update notification is coalesced by the browser.
chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId === 0) refreshActiveTab();
});
chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  if (details.frameId === 0) refreshActiveTab();
});

chrome.runtime.onStartup.addListener(() => refreshActiveTab(true));
chrome.runtime.onInstalled.addListener(() => refreshActiveTab(true));

connectNativeHost();
