/**
 * Clipy Tailwind build.
 *
 * Rebuild the stylesheet after adding new utility classes to templates:
 *   > build-css.bat          (or)   npx tailwindcss -m -o static/vendor/tailwind.min.css
 */
const ink = {
  50: '#fafafa', 100: '#f4f4f5', 200: '#e4e4e7', 300: '#d4d4d8',
  400: '#a1a1aa', 500: '#71717a', 600: '#52525b', 700: '#3f3f46',
  800: '#27272a', 900: '#18181b', 950: '#09090b',
};

module.exports = {
  content: ['./templates/**/*.html'],
  theme: {
    extend: {
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] },
      colors: {
        // The app's custom near-black palette (alias of zinc values).
        ink,
      },
    },
  },
  corePlugins: { preflight: true },
};
