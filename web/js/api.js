// The one place the page talks to the server. The access token lives in this
// module's memory only: it is never written to browser storage, a cookie or the URL.
const BASE = "/api/v1";
let token = null;

export class ApiError extends Error {
  constructor(status, envelope) {
    const err = (envelope && envelope.error) || {};
    super(err.message || `request failed (${status})`);
    this.status = status;
    this.code = err.code || "error";
    this.details = err.details || {};
    this.requestId = err.request_id || "";
  }
}

export function setToken(value) { token = value || null; }
export function hasToken() { return token !== null; }

export async function api(method, path, body) {
  if (!token) throw new ApiError(401, { error: { code: "unauthenticated", message: "not connected" } });
  const init = { method, headers: { Authorization: `Bearer ${token}` }, credentials: "omit", cache: "no-store" };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(BASE + path, init);
  if (resp.status === 204) return null;
  let data = null;
  try { data = await resp.json(); } catch { data = null; }
  if (!resp.ok) throw new ApiError(resp.status, data);
  return data;
}
