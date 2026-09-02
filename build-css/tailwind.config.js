/**
 * Builds the static Tailwind stylesheet that replaces the cdn.tailwindcss.com
 * script. The CDN build compiles classes in the browser with new Function(),
 * which a Content-Security-Policy blocks outright and which prints a
 * "should not be used in production" warning. Scanning the HTML/JS here and
 * shipping plain CSS removes both problems.
 *
 * Rebuild after adding new utility classes:  npm run build:css
 */
module.exports = {
  content: ['../index.html', '../map.html', '../js/**/*.js'],
  theme: { extend: {} },
  plugins: [],
};
