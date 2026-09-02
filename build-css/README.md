# Tailwind stylesheet build

`../tailwind.css` is a committed, pre-compiled Tailwind build. It replaces the
`cdn.tailwindcss.com` script, which compiled classes in the browser using
`new Function()` — blocked outright by a Content-Security-Policy, and the
source of Tailwind's "should not be used in production" console warning.

The generated CSS is committed so GitHub Pages can serve the site directly
with no build step.

## Rebuild after adding new utility classes

```bash
cd build-css
npm install
npm run build:css
```

`tailwind.config.js` scans `index.html`, `map.html`, `diagnose.html` and
`js/**/*.js`. Classes assembled from string fragments at runtime cannot be
detected by that scan — write them out in full, or add them to a `safelist`
in the config.
