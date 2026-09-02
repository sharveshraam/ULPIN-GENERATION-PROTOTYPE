/**
 * ============================================================
 *  BACKEND ENDPOINT — the only place the API URL is configured
 * ============================================================
 *
 * Paste your deployed backend URL here, with NO trailing slash:
 *
 *   const API_BASE_URL = 'https://ulpin-api.onrender.com';
 *
 * Leave it as an empty string to fall back to:
 *   - http://127.0.0.1:8000  when opened on localhost (local development)
 *   - same origin            anywhere else
 *
 * This is the single source of truth. There is no query-parameter override
 * and no in-app dialog: the endpoint is fixed in code, so whatever is
 * committed here is exactly what the deployed site talks to.
 *
 * The value is a public API endpoint, not a secret — safe to commit.
 *
 * NOTE: this URL is ignored when the page is already served by the backend
 * (https://ulpin-generation-prototype.onrender.com/app/). There the client
 * uses relative URLs, so requests are first-party. That matters because
 * browser tracking prevention and privacy extensions block on the cross-site
 * relationship — a call from github.io to onrender.com is third-party and
 * gets cancelled before it is sent, while the same call from a page on
 * onrender.com is not. Use /app/ if an ad blocker is interfering.
 */
const API_BASE_URL = 'https://ulpin-generation-prototype.onrender.com';

window.API_BASE_URL = API_BASE_URL;
