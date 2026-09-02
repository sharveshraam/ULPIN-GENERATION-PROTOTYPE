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
 */
const API_BASE_URL = '';

window.API_BASE_URL = API_BASE_URL;
