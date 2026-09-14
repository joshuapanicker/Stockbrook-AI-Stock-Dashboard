/** @type {import('tailwindcss').Config} */
// Palette discipline (the rule the landing page is built on):
//
//   accent  → brand. Buttons, links, active states, the logo mark.
//   green   → price up / gains / passing a rule. Nothing else.
//   red     → price down / losses / failing a rule. Nothing else.
//
// Red is never a brand color here. In a financial interface red already
// means "you lost money", so spending it on a CTA tells the user the wrong
// thing on every primary action. Terminal blue carries the brand instead —
// the same convention Bloomberg, Koyfin and every broker UI follows.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0B0D12",
        card: "#10131A",
        card2: "#171C29",
        border: "#272E40",

        // Brand — the app's neon teal-green. Same value as `green` below:
        // in this product "positive" and "brand" are deliberately the same
        // color, which is why accent-on-dark reads as a gain, not a warning.
        // Anything sitting ON accent uses the near-black `bg` as its ink —
        // white on #2EE6A8 is about 1.9:1 and fails badly.
        accent: {
          DEFAULT: "#2EE6A8",
          bright: "#5BF0C0",
          dim: "#1FC48D",
          deep: "#17A376",
        },

        // Semantic price colors — direction only, never decoration
        green: { DEFAULT: "#2EE6A8", dim: "#1FC48D" },
        red: { DEFAULT: "#FF5C7A", dim: "#E23D5C" },

        // Retained for the authenticated app's existing UI
        purple: { DEFAULT: "#8055F5", dim: "#6A45D9" },
        orange: { DEFAULT: "#FFAC26" },
        sky: { DEFAULT: "#3FA7FC" },
        muted: "#6E7787",
      },
      fontFamily: {
        // Two voices, and only two: Inter for language, JetBrains Mono for
        // anything that is a number, a ticker, or a label. The display serif
        // and the two extra grotesques are gone — a terminal doesn't set its
        // headlines in a fashion-magazine serif.
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "monospace"],
        display: ["Inter", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
