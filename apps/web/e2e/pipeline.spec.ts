import { expect, test, type Locator } from "@playwright/test";
import { execFileSync } from "node:child_process";
import path from "node:path";

/** Range inputs cannot be typed into: set the value the way a drag would, with real input events. */
const setRange = (slider: Locator, value: string) =>
  slider.evaluate((element, next) => {
    const input = element as HTMLInputElement;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, next);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }, value);

// TEST-ADAPTER RUN: proves the application workflow end to end in a browser.
// Media work (probe, cut, Remotion render, validation) is real; AI analysis and transcription are simulated.

test("local video → clip rendered with subtitles → shown, editable and downloadable", async ({ page }, testInfo) => {
  const fixture = path.join(testInfo.outputDir, "fixture.mp4");
  execFileSync("mkdir", ["-p", testInfo.outputDir]);
  execFileSync("ffmpeg", [
    "-hide_banner",
    "-loglevel",
    "error",
    "-y",
    "-f",
    "lavfi",
    "-i",
    "testsrc2=size=640x360:rate=30:duration=20",
    "-f",
    "lavfi",
    "-i",
    "sine=frequency=440:duration=20",
    "-c:v",
    "libx264",
    "-preset",
    "ultrafast",
    "-pix_fmt",
    "yuv420p",
    "-c:a",
    "aac",
    fixture,
  ]);

  await page.goto("/");
  await expect(page.getByText("MODE TEST")).toBeVisible(); // the interface says it is not doing real inference
  await page.getByRole("link", { name: "Nouveau projet" }).first().click();

  // 1. import a local fixture — 2. real media metadata is shown
  await page.getByRole("radio", { name: "Fichier local" }).click();
  await page.locator('input[type="file"]').setInputFiles(fixture);
  await expect(page.getByText(/640×360/)).toBeVisible();
  await expect(page.getByText(/h264\/aac/)).toBeVisible();

  await page.getByLabel("Durée min (s)").fill("6");
  await page.getByLabel("Durée max (s)").fill("12");
  await setRange(page.locator('input[type="range"]').first(), "1"); // one clip
  await setRange(page.locator('input[type="range"]').nth(1), "0"); // no quality threshold: the test adapter's marks are arbitrary
  await page.getByRole("radio", { name: "Rapide" }).click();
  await page.getByRole("button", { name: "Générer les clips" }).click();

  // 3–6. the pipeline runs for real in the worker; every stage must end green
  await expect(page).toHaveURL(/\/projets\/[0-9a-f]{32}$/);
  await expect(page.getByText("Journal d'activité")).toBeVisible();
  const card = page.locator("article").first();
  await expect(card.getByText("Rendu validé")).toBeVisible({ timeout: 240_000 });
  await expect(page.getByText(/export\(s\) validé\(s\)/)).toBeVisible(); // the validation stage reports what it checked
  await expect(page.getByText("ADAPTATEUR DE TEST").first()).toBeVisible();

  // 7. the rendered clip is displayed through the application
  await expect(card.getByText("TEST", { exact: true }).first()).toBeVisible();
  const video = card.locator("video");
  await expect(video).toHaveJSProperty("videoWidth", 1080);
  await expect(video).toHaveJSProperty("videoHeight", 1920);

  // download works and is a real MP4
  const downloadHref = await card.getByRole("link", { name: "Télécharger le MP4" }).getAttribute("href");
  const response = await page.request.get(downloadHref!);
  expect(response.status()).toBe(200);
  expect(response.headers()["content-type"]).toBe("video/mp4");
  expect((await response.body()).byteLength).toBeGreaterThan(50_000);

  // the editor opens on that clip and can save a subtitle correction
  await card.getByRole("link", { name: "Modifier" }).click();
  await expect(page.getByText("Texte à l'écran")).toBeVisible();
  await page.getByRole("radio", { name: "Clean" }).click();
  await expect(page.getByText("Modifications non enregistrées")).toBeVisible();
  await page.getByRole("button", { name: "Enregistrer", exact: true }).click();
  await expect(page.getByText("À re-rendre").first()).toBeVisible();
});
