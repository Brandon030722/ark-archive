/* Exercise both .cmd files and the packaged app on a fresh Windows VM. */
const assert = require("node:assert/strict");
const { spawn, spawnSync } = require("node:child_process");
const { createHash } = require("node:crypto");
const { existsSync, readFileSync } = require("node:fs");
const path = require("node:path");
const { setTimeout: delay } = require("node:timers/promises");
const { chromium } = require(process.env.ARK_PLAYWRIGHT_CORE);

for (const file of [".python-version", "docs/运行手册.md", "docs/资料来源.md"]) {
  assert(existsSync(file), `Expected file missing after Windows extraction: ${file}`);
}
const archive = path.join(process.cwd(), ".local");
const host = "http://127.0.0.1:18086";
const manifest = JSON.parse(readFileSync("distribution/knowledge.json", "utf8"));
const token = "synthetic-windows-onboarding-only";
const digest = (file) => createHash("sha256").update(readFileSync(path.join(archive, file))).digest("hex");

function command(file, args = []) {
  // cmd.exe invokes the same entry point that Explorer uses when opened.
  return spawn(process.env.ComSpec, ["/d", "/c", file, ...args], { stdio: "inherit" });
}
function start() {
  return command("Start-Ark-Archive.cmd", ["-Port", "18086", "-NoBrowser"]);
}
function exited(child, allowAnyStatus = false) {
  return new Promise((resolve, reject) => {
    const done = (code) => {
      clearTimeout(timer);
      if (code === 0 || allowAnyStatus) resolve();
      else reject(new Error(`Launcher exited with code ${code}`));
    };
    const timer = setTimeout(() => reject(new Error("Launcher did not stop")), 45000);
    if (child.exitCode !== null) done(child.exitCode);
    else child.once("exit", done);
  });
}
async function read(endpoint) {
  const response = await fetch(host + endpoint, { signal: AbortSignal.timeout(5000) });
  assert(response.ok, `${endpoint} returned ${response.status}`);
  return response.json();
}
async function ready(child) {
  for (let attempt = 0; attempt < 180; attempt++) {
    if (child.exitCode !== null) throw new Error(`Startup exited with ${child.exitCode}`);
    const active = await read("/v1/releases/active").catch(() => null);
    if (active) {
      assert.equal(active.id, manifest.release);
      return;
    }
    await delay(2000);
  }
  throw new Error("The initial install did not become ready");
}
async function stop(child) {
  if (!child || child.exitCode !== null) return;
  await exited(command("Stop-Ark-Archive.cmd"));
  await exited(child);
  await assert.rejects(fetch(host + "/health", { signal: AbortSignal.timeout(3000) }));
}
function checkSeedAndLedger() {
  const script = [
    "import json, os, sqlite3",
    "from pathlib import Path",
    "from urllib.parse import urlencode",
    "from scripts.check_distribution import main",
    "params = json.loads(Path('.local/postgres/data/ark-connection.json').read_text())",
    "url = 'postgresql+psycopg:///arknights?' + urlencode(dict(params, host='127.0.0.1', user='ark_archive'))",
    "os.environ['ARK_DATABASE_URL'] = os.environ['ARK_TEST_DATABASE_URL'] = url",
    "main()",
    "with sqlite3.connect('.local/model-budget.sqlite3') as ledger:",
    "    assert ledger.execute('SELECT count(*) FROM reservation').fetchone() == (0,)",
  ].join("\n");
  const result = spawnSync(".venv\\Scripts\\python.exe", ["-c", script], {
    stdio: "inherit", env: { ...process.env, PYTHONUTF8: "1" },
  });
  assert.equal(result.status, 0);
}
async function exercise() {
  let child = start();
  let browser;
  let run;
  try {
    await ready(child);
    assert.equal((await read("/v1/agent/status")).web_model.reason, "configuration_missing");
    browser = await chromium.launch({ channel: "msedge", headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 860 } });
    await context.route("**/*", (route) => new URL(route.request().url()).origin === host
      ? route.continue() : route.abort());
    const page = await context.newPage();
    await page.goto(host);
    for (let index = 1; index <= 6; index++) {
      const tour = page.locator("#workbench-tour[open]");
      await tour.waitFor();
      assert.equal((await tour.locator("#tour-count").innerText()).replace(/\s/g, ""), `${index}/6`);
      await tour.locator("#tour-next").click();
    }
    await page.locator("#credential-dialog[open] #credential-understand").click();
    await page.locator("#provider-dialog[open] [data-provider=compatible]").click();
    await page.locator("#provider-url").fill("https://example.invalid/v1");
    await page.locator("#provider-token").fill(token);
    await page.locator("#provider-model-choice").selectOption("__custom__");
    await page.locator("#provider-model").fill("windows-smoke");
    for (const [id, value] of Object.entries({
      context: "32768", output: "2048", "input-price": "3", "output-price": "12",
    })) {
      await page.locator("#provider-" + id).fill(value);
    }
    const saved = page.waitForResponse((response) =>
      response.url().endsWith("/v1/account/providers") && response.request().method() === "POST");
    await page.locator("#provider-save").click();
    assert((await saved).ok(), await page.locator("#provider-feedback").innerText());
    assert((await read("/v1/agent/status")).web_model.ready);
    assert(!readFileSync(path.join(archive, "credentials", "providers.enc")).includes(token));
    assert((await read("/v1/account/credential")).tour_pending === false);
    assert.equal((await read("/v1/account/providers")).connections.compatible.model, "windows-smoke");
    const recruitment = await context.request.post(host + "/v1/recruitment/calculate", {
      data: { tags: ["高级资深干员", "位移"], duration_minutes: 540 },
    });
    assert(recruitment.ok());
    run = await recruitment.json();
    const selected = run.combinations.find((row) => row.selected_tags.length === 2);
    assert.deepEqual(selected.candidates.map((item) => item.name), ["温蒂"]);
    const citation = run.citations.find((item) => item.record_id === selected.candidates[0].record_id);
    await read("/v1/evidence/" + citation.evidence_id);
    await read("/v1/snapshots/" + citation.snapshot_id);
    checkSeedAndLedger();
    const remembered = ["credentials/master.key", "credentials/providers.enc", "model-budget.sqlite3"]
      .map(digest);
    await context.close();
    await stop(child);

    child = start();
    await ready(child);
    assert((await read("/v1/agent/status")).web_model.ready);
    assert.equal((await read("/v1/account/providers")).connections.compatible.model, "windows-smoke");
    assert.equal((await read("/v1/account/credential")).tour_pending, false);
    await read(`/v1/qa/runs/${run.run_id}?release_id=${run.release_id}`);
    assert.deepEqual(["credentials/master.key", "credentials/providers.enc", "model-budget.sqlite3"]
      .map(digest), remembered);
    console.log("Windows clean install passed: seed, six-step tour, first key, citation, restart, ledger 0.");
  } finally {
    if (browser) await browser.close();
    await stop(child);
  }
}
exercise().catch((error) => { console.error(error); process.exitCode = 1; });
