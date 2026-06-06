/**
 * Gain Bridge — Node for Max
 * Connects Max for Live to Gain's behavioral mixer API.
 * Gain must be running on port 5570.
 */

const maxApi = require("max-api");
const http   = require("http");

const GAIN_PORT = 5570;
const GAIN_HOST = "127.0.0.1";

function gainRequest(method, path, body, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const payload = body ? JSON.stringify(body) : null;
    const options = {
      hostname: GAIN_HOST,
      port: GAIN_PORT,
      path,
      method,
      headers: {
        "Content-Type": "application/json",
        ...(payload ? { "Content-Length": Buffer.byteLength(payload) } : {}),
      },
    };
    const req = http.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try { resolve(JSON.parse(data)); }
        catch (e) { resolve({ text: data }); }
      });
    });
    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error("Gain server timeout — is Gain running on port 5570?"));
    });
    req.on("error", reject);
    if (payload) req.write(payload);
    req.end();
  });
}

// ── Handlers ────────────────────────────────────────────────────────────────

maxApi.addHandler("getstate", async () => {
  try {
    const state = await gainRequest("GET", "/m4l/state");
    maxApi.outlet("mode",      state.mode      || "BUILD");
    maxApi.outlet("intensity", state.intensity || 0.5);
    maxApi.outlet("depth",     state.depth     || 0.5);
    maxApi.outlet("room",      state.room      || 0.5);
    maxApi.outlet("status",    "ready");
  } catch (e) {
    maxApi.outlet("status", "error: gain not running");
  }
});

maxApi.addHandler("ask", async (...args) => {
  const prompt = args.join(" ").trim();
  if (!prompt) return;
  maxApi.outlet("status", "thinking...");
  try {
    const res = await gainRequest("POST", "/m4l/ask", { prompt }, 60000);
    if (res.error) {
      maxApi.outlet("status", "error: " + res.error);
    } else {
      maxApi.outlet("response", res.text);
      maxApi.outlet("tokens",   res.tokens || 0);
      maxApi.outlet("status",   "done · " + (res.tokens || 0) + " tokens");
    }
  } catch (e) {
    maxApi.outlet("status", "error: gain not running");
  }
});

maxApi.addHandler("askpreset", async (...args) => {
  const preset = args[0];
  const prompt = args.slice(1).join(" ").trim();
  if (!prompt) return;
  maxApi.outlet("status", "thinking with preset: " + preset + "...");
  try {
    const res = await gainRequest("POST", "/m4l/ask", { prompt, preset }, 60000);
    if (res.error) {
      maxApi.outlet("status", "error: " + res.error);
    } else {
      maxApi.outlet("response", res.text);
      maxApi.outlet("tokens",   res.tokens || 0);
      maxApi.outlet("status",   "done · " + (res.tokens || 0) + " tokens");
    }
  } catch (e) {
    maxApi.outlet("status", "error: " + e.message);
  }
});

maxApi.addHandler("setfader", async (field, value) => {
  try {
    await gainRequest("POST", "/set", { [field]: parseFloat(value) });
    maxApi.outlet("status", field + " → " + value);
  } catch (e) {
    maxApi.outlet("status", "error: " + e.message);
  }
});

maxApi.addHandler("setmode", async (mode) => {
  try {
    await gainRequest("POST", "/set", { mode: mode.toUpperCase() });
    maxApi.outlet("mode",   mode.toUpperCase());
    maxApi.outlet("status", "mode → " + mode.toUpperCase());
  } catch (e) {
    maxApi.outlet("status", "error: " + e.message);
  }
});

maxApi.addHandler("getpresets", async () => {
  try {
    const presets = await gainRequest("GET", "/m4l/presets");
    maxApi.outlet("presets", presets);
  } catch (e) {
    maxApi.outlet("presets", []);
  }
});

maxApi.post("Gain Bridge loaded. Make sure Gain is running on port 5570.");
