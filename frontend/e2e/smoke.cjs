// Browser smoke test of the demo flows (claim race, live updates, edit conflict + merge, approvals,
// flaky-network replay, reporter view, notifications, jobs page, create).
//
// Optional — not part of the default test run. Requires a FRESHLY SEEDED server:
//   python run.py --reset          (in one terminal)
//   cd frontend && npm i --no-save playwright && npx playwright install chromium
//   node e2e/smoke.cjs             (screenshots go to frontend/e2e/screenshots/)
const { chromium } = require("playwright");
const OUT = process.env.SHOTS_DIR || require("path").join(__dirname, "screenshots");
const BASE = process.env.BASE_URL || "http://localhost:8000";
require("fs").mkdirSync(OUT, { recursive: true });
let failures = 0;
function check(cond, msg) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${msg}`);
  if (!cond) failures++;
}

async function login(browser, user) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto(BASE + "/");
  await page.getByRole("button", { name: new RegExp(user) }).first().click();
  await page.waitForSelector(".sidebar");
  page._errors = errors;
  return page;
}

(async () => {
  const browser = await chromium.launch();
  const log = (...a) => console.log(...a);

  // 1. Dashboard
  const priya = await login(browser, "Priya Sharma");
  await priya.waitForSelector(".tile");
  await priya.waitForTimeout(600);
  await priya.screenshot({ path: `${OUT}/01-dashboard.png`, fullPage: false });
  log("dashboard tiles:", await priya.locator(".tile h3").allTextContents());

  // 2. List + search
  await priya.goto(BASE + "/items");
  await priya.waitForSelector("table.items tbody tr");
  log("list rows:", await priya.locator("table.items tbody tr").count());
  await priya.fill("input.search", "invoices");
  await priya.waitForTimeout(900);
  log("search url:", priya.url(), "rows:", await priya.locator("table.items tbody tr").count());
  await priya.screenshot({ path: `${OUT}/02-list-search.png` });
  await priya.getByRole("button", { name: "Load more" }).click().catch(() => {});
  await priya.waitForTimeout(500);
  log("after load more rows:", await priya.locator("table.items tbody tr").count());

  // 3. Claim race on the unowned P1 incident: Neha and Kabir both in SRE
  const neha = await login(browser, "Neha Kulkarni");
  const kabir = await login(browser, "Kabir Singh");
  const r = await neha.request.get(BASE + "/api/items?q=Checkout%20API%20502");
  const incident = (await r.json()).items[0];
  log("incident", incident.id, incident.status, incident.assignee);
  await Promise.all([neha.goto(`${BASE}/items/${incident.id}`), kabir.goto(`${BASE}/items/${incident.id}`)]);
  await Promise.all([neha.waitForSelector(".actionbar"), kabir.waitForSelector(".actionbar")]);
  await Promise.all([
    neha.getByRole("button", { name: "Claim", exact: true }).click(),
    kabir.getByRole("button", { name: "Claim", exact: true }).click(),
  ]);
  await neha.waitForTimeout(1500);
  log("neha toasts:", await neha.locator(".toast").allTextContents());
  log("kabir toasts:", await kabir.locator(".toast").allTextContents());
  const owners = [await neha.locator(".detail-side dd").nth(1).textContent(), await kabir.locator(".detail-side dd").nth(1).textContent()];
  log("owners:", owners);
  const wins = [...(await neha.locator(".toast").allTextContents()), ...(await kabir.locator(".toast").allTextContents())].filter((t) => t.includes("You own this now")).length;
  check(wins === 1, "claim race: exactly one winner");
  check(owners[0] === owners[1], "claim race: both screens show the same owner");
  await kabir.screenshot({ path: `${OUT}/03-claim-race-loser.png` });

  // 4. Live update: Neha comments, Kabir's page should update via SSE
  await neha.fill(".composer textarea", "Rolling back the 09:00 deploy now.");
  await neha.getByRole("button", { name: "Comment" }).click();
  await kabir.waitForSelector("text=Rolling back the 09:00 deploy now.", { timeout: 6000 });
  check(true, "live update: other user's comment appeared without reload");
  await kabir.screenshot({ path: `${OUT}/04-live-comment.png` });

  // 5. Edit conflict: both edit the title from the same version
  await Promise.all([neha.getByRole("button", { name: "Edit" }).click(), kabir.getByRole("button", { name: "Edit" }).click()]);
  await neha.fill(".edit input >> nth=0", "Checkout API 502s: rollback in progress");
  await kabir.fill(".edit input >> nth=0", "Checkout API 502s after 09:00 deploy");
  await neha.getByRole("button", { name: "Save changes" }).click();
  await neha.waitForTimeout(1800); // let kabir receive the live event
  await kabir.screenshot({ path: `${OUT}/05a-editing-stale-banner.png` });
  await kabir.getByRole("button", { name: "Save changes" }).click();
  await kabir.waitForSelector(".modal", { timeout: 5000 });
  log("conflict dialog:", (await kabir.locator(".modal").innerText()).slice(0, 220).replace(/\n/g, " | "));
  await kabir.screenshot({ path: `${OUT}/05b-conflict-dialog.png` });
  await kabir.getByRole("button", { name: "Keep editing" }).click();
  await kabir.getByRole("button", { name: "Cancel" }).click();

  // 6. Non-overlapping concurrent edits merge (priority vs description)
  await Promise.all([neha.reload(), kabir.reload()]);
  await Promise.all([neha.waitForSelector(".actionbar"), kabir.waitForSelector(".actionbar")]);
  await Promise.all([neha.getByRole("button", { name: "Edit" }).click(), kabir.getByRole("button", { name: "Edit" }).click()]);
  await neha.selectOption(".edit select >> nth=0", "2");
  await kabir.fill(".edit textarea", "Alert fired at 09:12. Rollback started 09:20 by Neha.");
  await neha.getByRole("button", { name: "Save changes" }).click();
  await neha.waitForTimeout(400);
  await kabir.getByRole("button", { name: "Save changes" }).click();
  await kabir.waitForTimeout(800);
  check((await kabir.locator(".toast").allTextContents()).some((t) => t.includes("Merged")), "disjoint concurrent edits merge");

  // 7. Approval flow: Meera approves Arjun's refund; Arjun's own page has no Approve button
  const meera = await login(browser, "Meera Iyer");
  await meera.goto(BASE + "/items?view=awaiting_my_approval");
  await meera.waitForSelector("table.items tbody tr");
  await meera.locator("a.item-link", { hasText: "Refund INR 48,000" }).click();
  await meera.waitForSelector(".actionbar");
  log("meera actions:", await meera.locator(".actionbar button").allTextContents());
  await meera.getByRole("button", { name: "Approve" }).click();
  await meera.waitForTimeout(800);
  check((await meera.locator(".detail-badges .status").textContent()) === "Approved", "lead can approve");
  await meera.screenshot({ path: `${OUT}/06-approved.png` });

  // 8. Flaky network: response lost, retried with same key, no duplicate comment
  const arjun = await login(browser, "Arjun Nair");
  await arjun.goto(meera.url());
  await arjun.waitForSelector(".actionbar");
  check(!(await arjun.locator(".actionbar button").allTextContents()).includes("Approve"), "owner has no Approve button");
  await arjun.check(".chaos input");
  await arjun.fill(".composer textarea", "Refund executed. Bank ref UTR998877.");
  await arjun.getByRole("button", { name: "Comment" }).click();
  await arjun.waitForTimeout(2000);
  log("arjun toasts:", await arjun.locator(".toast").allTextContents());
  const count = await arjun.locator(".comment-text", { hasText: "UTR998877" }).count();
  check(count === 1, "lost response + retry posts exactly one comment");

  // 9. Sam (no team) sees only his requests; viewer-only actions
  const sam = await login(browser, "Sam Thomas");
  await sam.goto(BASE + "/items");
  await sam.waitForSelector("table.items tbody tr, .empty");
  check((await sam.locator("table.items tbody tr").count()) === 1, "teamless reporter sees only their own request");
  await sam.locator("a.item-link").first().click();
  await sam.waitForSelector(".actionbar");
  log("sam actions:", await sam.locator(".actionbar button").allTextContents());
  await sam.screenshot({ path: `${OUT}/07-reporter-view.png` });

  // 10. Notifications + admin jobs
  await meera.goto(BASE + "/notifications");
  await meera.waitForTimeout(500);
  await meera.screenshot({ path: `${OUT}/08-notifications.png` });
  const admin = await login(browser, "Asha Admin");
  await admin.goto(BASE + "/admin/jobs");
  await admin.waitForSelector(".stats");
  await admin.screenshot({ path: `${OUT}/09-jobs.png` });

  // 11. New item
  await priya.goto(BASE + "/items/new");
  await priya.waitForSelector("form.form");
  await priya.fill("form.form input >> nth=0", "Customer reports wrong GST on invoice");
  await priya.screenshot({ path: `${OUT}/10-new-item.png` });
  await priya.getByRole("button", { name: "Create work item" }).click();
  await priya.waitForURL(/\/items\/\d+$/);
  log("created:", priya.url());

  for (const [n, p] of Object.entries({ priya, neha, kabir, meera, arjun, sam, admin })) {
    if (p._errors.length) log("JS errors for", n, p._errors);
  }
  await browser.close();
  console.log(failures ? `\n${failures} check(s) failed` : "\nall checks passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
