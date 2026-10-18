"use strict";

export function canonicalStringify(value) {
  if (value === null || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
  if (typeof value === "string") {
    for (let i = 0; i < value.length; i++) {
      const code = value.charCodeAt(i);
      if (code >= 0xd800 && code <= 0xdbff) {
        const next = value.charCodeAt(++i);
        if (!(next >= 0xdc00 && next <= 0xdfff)) throw Error("INVALID_UNICODE");
      } else if (code >= 0xdc00 && code <= 0xdfff) throw Error("INVALID_UNICODE");
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return "[" + value.map(canonicalStringify).join(",") + "]";
  if (value && Object.getPrototypeOf(value) === Object.prototype) {
    const keys = Object.keys(value).sort();
    if (keys.some(key => !/^[\x20-\x7e]+$/.test(key))) throw Error("INVALID_CANONICAL_KEY");
    return "{" + keys.map(key => JSON.stringify(key) + ":" + canonicalStringify(value[key])).join(",") + "}";
  }
  throw Error("UNSUPPORTED_CANONICAL_VALUE");
}

export async function contentDigest(value) {
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonicalStringify(value)));
  return Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, "0")).join("");
}

export async function checkEnvelope(proof) {
  const {proof_digest, ...fields} = proof;
  if (await contentDigest(fields) !== proof_digest) throw Error("PROOF_DIGEST_MISMATCH");
  if (await contentDigest(proof.baseline) !== proof.baseline_digest || await contentDigest(proof.current) !== proof.current_digest || await contentDigest(proof.evidence) !== proof.evidence_root) throw Error("CONTENT_DIGEST_MISMATCH");
  let previous = "0".repeat(64);
  for (const [index, event] of proof.events.entries()) {
    const {hash, ...body} = event;
    if (body.sequence !== index + 1 || body.previous !== previous || await contentDigest(body) !== hash) throw Error("JOURNAL_MISMATCH");
    previous = hash;
  }
  if (previous !== proof.journal_head || proof.action_status !== "WITHHELD" || proof.release_authorized !== false || proof.external_action_executed !== false) throw Error("CONTROL_BOUNDARY_MISMATCH");
  return {scope: "HASH_CONSISTENCY_ONLY", signature: "UNSIGNED_CONTROL_RECORD"};
}

export class ControlClient {
  constructor({origin = location.origin, token = null, sessionId = null} = {}) {
    const url = new URL(origin);
    if (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname))) throw Error("HTTPS_REQUIRED");
    if (url.username || url.password || url.search || url.hash || url.pathname !== "/") throw Error("ORIGIN_REQUIRED");
    this.origin = url.origin;
    this.token = token;
    this.sessionId = sessionId;
  }
  async request(path, payload) {
    if (!path.startsWith("/api/control/")) throw Error("CONTROL_PATH_REQUIRED");
    const headers = {Accept: "application/json"};
    if (this.token) headers.Authorization = "Bearer " + this.token;
    if (this.sessionId) headers["X-OFC-Session"] = this.sessionId;
    const options = {headers, cache: "no-store", credentials: "same-origin", redirect: "error", signal: AbortSignal.timeout(15000)};
    if (payload !== undefined) {
      options.method = "POST";
      headers["Content-Type"] = "application/json";
      options.body = canonicalStringify(payload);
    }
    const response = await fetch(this.origin + path, options);
    const body = await response.text();
    if (body.length > 2000000) throw Error("RESPONSE_TOO_LARGE");
    const value = JSON.parse(body);
    if (!response.ok) {
      const error = Error(value.error || "REVIEW_UNAVAILABLE");
      error.fields = value.validation_errors || [];
      throw error;
    }
    return value;
  }
  submit(request) { return this.request("/api/control/plans", request); }
  status(id) { return this.request(this.path(id)); }
  proof(id) { return this.request(this.path(id) + "/proof"); }
  events(id, after = 0) { return this.request(this.path(id) + "/events?after=" + after); }
  path(id) {
    if (!/^pr-[a-f0-9]{32}$/.test(id)) throw Error("INVALID_REVIEW_ID");
    return "/api/control/reviews/" + id;
  }
}
