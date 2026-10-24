/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Archivo Variable"', "system-ui", "-apple-system", '"Segoe UI"', "sans-serif"],
      },
      colors: {
        page: "rgb(var(--page) / <alpha-value>)",
        surface: "rgb(var(--surface) / <alpha-value>)",
        ink: "rgb(var(--ink) / <alpha-value>)",
        "ink-2": "rgb(var(--ink-2) / <alpha-value>)",
        muted: "rgb(var(--muted) / <alpha-value>)",
        hairline: "rgb(var(--hairline) / <alpha-value>)",
        track: "rgb(var(--track) / <alpha-value>)",
        focus: "rgb(var(--focus) / <alpha-value>)",
        good: "rgb(var(--good) / <alpha-value>)",
        warn: "rgb(var(--warn) / <alpha-value>)",
        critical: "rgb(var(--critical) / <alpha-value>)",
      },
      fontSize: {
        readout: ["clamp(3rem, 9vw, 5.5rem)", { lineHeight: "0.95", letterSpacing: "-0.02em" }],
      },
    },
  },
  plugins: [],
};
