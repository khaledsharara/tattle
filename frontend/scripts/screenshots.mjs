/**
 * Draws the README screenshots from the demo's made-up household, light and dark:
 *
 *   docker run --rm -p 8090:8090 -e TATTLE_DEMO=1 ghcr.io/khaledsharara/tattle
 *   npm run screenshots          # TATTLE_URL=http://localhost:8090 by default
 *
 * Uses the Chrome installed on the machine (PLAYWRIGHT_CHANNEL=chromium for
 * Playwright's own). Output: docs/screenshots/<shot>-<light|dark>.png
 */
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const BASE = process.env.TATTLE_URL ?? "http://localhost:8090";
const OUT = fileURLToPath(new URL("../../docs/screenshots/", import.meta.url));
const CHANNEL = process.env.PLAYWRIGHT_CHANNEL ?? "chrome";

const DESKTOP = { viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 };
const PHONE = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true };

/** name, window, and what to do before the picture */
const SHOTS = [
  { name: "overview", size: DESKTOP, go: (page) => page.goto(`${BASE}/`) },
  { name: "overview-phone", size: PHONE, go: (page) => page.goto(`${BASE}/`) },
  { name: "devices", size: DESKTOP, go: (page) => page.goto(`${BASE}/devices?range=7d`) },
  {
    name: "device",
    size: DESKTOP,
    go: async (page) => {
      await page.goto(`${BASE}/devices?range=7d`);
      await page.getByRole("link", { name: /Maya's iPhone/ }).first().click();
    },
  },
  { name: "line", size: DESKTOP, go: (page) => page.goto(`${BASE}/isp/HOME-4821`) },
  {
    name: "connect",
    size: { ...DESKTOP, viewport: { width: 900, height: 900 } },
    element: "#provider",
    go: async (page) => {
      await page.goto(`${BASE}/settings`);
      await page.locator("#provider select").selectOption("we_eg");
    },
  },
];

mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch({ channel: CHANNEL === "chromium" ? undefined : CHANNEL });
try {
  for (const scheme of ["light", "dark"]) {
    for (const shot of SHOTS) {
      const context = await browser.newContext({ ...shot.size, colorScheme: scheme });
      const page = await context.newPage();
      await shot.go(page);
      await page.waitForLoadState("networkidle");
      // The demo banner says what it is; the pictures speak for themselves.
      await page.addStyleTag({ content: "[data-demo-banner]{display:none !important}" });
      await page.mouse.move(0, 0); // no hover tooltips in the picture
      await page.waitForTimeout(700);
      const path = `${OUT}${shot.name}-${scheme}.png`;
      if (shot.element) await page.locator(shot.element).screenshot({ path });
      else await page.screenshot({ path });
      console.log(`  ${shot.name}-${scheme}.png`);
      await context.close();
    }
  }
} finally {
  await browser.close();
}
