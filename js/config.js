/**
 * Deployment configuration.
 *
 * ============================================================
 *  SET YOUR DEPLOYED BACKEND URL HERE
 * ============================================================
 * Paste the Render URL of your API below, with NO trailing slash, e.g.
 *
 *   const API_BASE_URL = 'https://ulpin-api.onrender.com';
 *
 * Leave it as an empty string to keep the old behaviour (localhost during
 * local development, same-origin otherwise).
 *
 * This value is only a DEFAULT. It is overridden, in priority order, by:
 *   1. an ?api=<url> query parameter
 *   2. a URL previously saved from the in-app "Connect API" dialog
 *      (stored in localStorage under 'ulpin_api_base')
 *
 * Nothing here is secret: it is a public API endpoint, safe to commit.
 */
const API_BASE_URL = '';

window.API_BASE_URL = API_BASE_URL;
