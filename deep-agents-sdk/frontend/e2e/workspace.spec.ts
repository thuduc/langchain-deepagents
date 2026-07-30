import { expect, test } from "@playwright/test";

test("development login opens the project workspace", async ({ page }) => {
  await page.goto("/");
  await page.getByPlaceholder("e.g. aludan").fill("playwright-workspace-user");
  await page.getByRole("button", { name: "Continue to workspace" }).click();
  await expect(page.getByRole("button", { name: "HPI Analytics", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "NMDB Analytics", exact: true })).toBeVisible();
  await expect(page.getByText("Model", { exact: true })).toBeVisible();
  await expect(page.getByText("Ask this project to investigate something new.")).toBeVisible();

  await page.getByRole("button", { name: "NMDB Analytics", exact: true }).click();
  await expect(page.locator(".workspace-title strong")).toHaveText("NMDB Analytics");
  await expect(page).toHaveURL(/\/projects\/nmdb-analytics\/new$/);
});

test("the workspace loads with no Content Security Policy violations", async ({ page }) => {
  // A build that inlines a font as a data: URI is blocked by `font-src 'self'`
  // and fails silently, so assert the console stays clean rather than trusting
  // that assets were emitted as files.
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(String(error)));

  await page.goto("/");
  await page.getByPlaceholder("e.g. aludan").fill("playwright-csp-user");
  await page.getByRole("button", { name: "Continue to workspace" }).click();
  await expect(page.getByRole("button", { name: "HPI Analytics", exact: true })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);

  const violations = consoleErrors.filter((text) => /Content Security Policy/i.test(text));
  expect(violations, `CSP violations: ${violations.join(" | ")}`).toEqual([]);
  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
  expect(pageErrors).toEqual([]);
});

test("submitting a prompt cannot open a second chat while the first is being created", async ({ page }) => {
  await page.goto("/");
  await page.getByPlaceholder("e.g. aludan").fill("playwright-double-submit-user");
  await page.getByRole("button", { name: "Continue to workspace" }).click();
  await page.getByRole("button", { name: "HPI Analytics", exact: true }).click();

  // Keep the run off the model, and widen the chat-creation window that the
  // composer previously stayed live through.
  await page.route("**/api/chat/stream", (route) => route.abort());
  const creations: string[] = [];
  await page.route("**/api/projects/*/sessions", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    creations.push(route.request().url());
    await new Promise((resolve) => setTimeout(resolve, 1500));
    return route.fallback();
  });

  const composer = page.locator(".composer textarea");
  await composer.fill("first prompt");
  await composer.press("Enter");

  // Read the state directly: an auto-retrying matcher would wait out the very
  // window this test exists to check, and pass even when the guard is missing.
  const lockedImmediately = await page.evaluate(() => {
    const field = document.querySelector<HTMLTextAreaElement>(".composer textarea");
    return field?.disabled === true;
  });
  expect(lockedImmediately, "composer stayed live while the chat was being created").toBe(true);

  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.keyboard.press("Enter");
    await page.waitForTimeout(100);
  }
  await page.waitForTimeout(2500);

  expect(creations).toHaveLength(1);
  const sessions = await page.context().request.get("/api/projects/hpi-analytics/sessions");
  expect((await sessions.json()).sessions).toHaveLength(1);
});

test("mobile project navigation opens and closes", async ({ page }) => {
  await page.setViewportSize({ width: 700, height: 900 });
  await page.goto("/");
  await page.getByPlaceholder("e.g. aludan").fill("playwright-mobile-user");
  await page.getByRole("button", { name: "Continue to workspace" }).click();

  await page.getByRole("button", { name: "Open sidebar" }).click();
  await expect(page.locator(".sidebar")).toHaveClass(/open/);
  await expect(page.getByRole("button", { name: "Close sidebar" })).toBeVisible();

  await page.getByRole("button", { name: "Close sidebar" }).click();
  await expect(page.locator(".sidebar")).not.toHaveClass(/open/);
});

test("project actions are complete and dismiss outside in every expansion state", async ({ page }) => {
  await page.goto("/");
  await page.getByPlaceholder("e.g. aludan").fill("playwright-project-menu-user");
  await page.getByRole("button", { name: "Continue to workspace" }).click();

  const hpiProject = page.getByRole("button", { name: "HPI Analytics", exact: true });
  const nmdbProject = page.getByRole("button", { name: "NMDB Analytics", exact: true });
  if (await hpiProject.getAttribute("aria-expanded") === "true") await hpiProject.click();
  if (await nmdbProject.getAttribute("aria-expanded") === "true") await nmdbProject.click();

  const trigger = page.getByTitle("Project actions for HPI Analytics");
  const menu = page.getByRole("menu", { name: "HPI Analytics actions" });
  await trigger.click();
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("menuitem", { name: "Contents", exact: true })).toBeInViewport();
  await expect(menu.getByRole("menuitem", { name: "Rename", exact: true })).toBeInViewport();
  await expect(menu.getByRole("menuitem", { name: "Import", exact: true })).toBeInViewport();
  await expect(menu.getByRole("menuitem", { name: "Export", exact: true })).toBeInViewport();

  const downloadPromise = page.waitForEvent("download");
  await menu.getByRole("menuitem", { name: "Export", exact: true }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("hpi-analytics.zip");

  await trigger.click();
  await page.locator(".workspace-title").click();
  await expect(menu).toHaveCount(0);

  await hpiProject.click();
  await nmdbProject.click();
  await trigger.click();
  await expect(menu.getByRole("menuitem", { name: "Contents", exact: true })).toBeInViewport();
  await expect(menu.getByRole("menuitem", { name: "Rename", exact: true })).toBeInViewport();
  await expect(menu.getByRole("menuitem", { name: "Import", exact: true })).toBeInViewport();
  await expect(menu.getByRole("menuitem", { name: "Export", exact: true })).toBeInViewport();

  await menu.getByRole("menuitem", { name: "Import", exact: true }).click();
  const importDialog = page.getByRole("dialog", { name: "Import Project Contents" });
  await expect(importDialog.getByRole("checkbox", { name: /Replace all current project content/ })).toBeVisible();
  await expect(importDialog.getByRole("checkbox", { name: /Replace all current project content/ })).not.toBeChecked();
  await importDialog.getByRole("button", { name: "Close" }).click();
});

test("project contents supports folder navigation, search, and rich previews", async ({ page }) => {
  await page.goto("/");
  await page.getByPlaceholder("e.g. aludan").fill("playwright-file-explorer-user");
  await page.getByRole("button", { name: "Continue to workspace" }).click();

  await page.getByTitle("Project actions for HPI Analytics").click();
  await page.getByRole("menuitem", { name: "Contents", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Project Contents" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("treeitem", { name: /^data$/ })).toBeVisible();
  await expect(dialog.getByRole("treeitem", { name: /^skills$/ })).toBeVisible();

  await dialog.getByRole("treeitem", { name: /^skills$/ }).click();
  await dialog.getByRole("treeitem", { name: /^hpi-analysis$/ }).click();
  await dialog.getByRole("treeitem", { name: /README\.md/ }).click();
  await expect(dialog.getByRole("heading", { name: "Testing the HPI Analysis Skill in Codex" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /Download/ })).toBeVisible();

  await dialog.getByRole("textbox", { name: "Search project files" }).fill("browser-preview-fixture");
  const imageResult = dialog.getByRole("treeitem", { name: /browser-preview-fixture\.png/ });
  await expect(imageResult).toBeVisible();
  await imageResult.click();
  await expect(dialog.getByRole("img", { name: /browser-preview-fixture\.png preview/ })).toBeVisible();

  await dialog.getByRole("textbox", { name: "Search project files" }).fill("");
  await dialog.getByRole("treeitem", { name: /^data$/ }).click();
  await dialog.getByRole("treeitem", { name: /hpi_master\.csv/ }).click();
  const table = dialog.getByRole("table");
  await expect(table).toBeVisible();
  await expect(table.getByRole("columnheader", { name: "hpi_type" })).toBeVisible();

  await dialog.getByRole("button", { name: "Close" }).click();
  await page.getByRole("button", { name: "NMDB Analytics", exact: true }).click();
  await page.getByTitle("Project actions for NMDB Analytics").click();
  await page.getByRole("menuitem", { name: "Contents", exact: true }).click();
  const nmdbDialog = page.getByRole("dialog", { name: "Project Contents" });
  await nmdbDialog.getByRole("textbox", { name: "Search project files" }).fill("technical-notes");
  await nmdbDialog.getByRole("treeitem", { name: /technical-notes\.pdf/ }).click();
  await expect(nmdbDialog.locator("canvas[aria-label*='technical-notes.pdf']")).toBeVisible();
  await expect(nmdbDialog.getByRole("button", { name: "Next PDF page" })).toBeEnabled();
  await nmdbDialog.getByRole("button", { name: "Next PDF page" }).click();
  await expect(nmdbDialog.getByText(/Page 2 of/)).toBeVisible();
});

test("project administrators can safely edit project contents", async ({ page }) => {
  const uniqueSuffix = Date.now();
  const projectName = `Explorer Edit ${uniqueSuffix}`;
  const projectId = `explorer-edit-${uniqueSuffix}`;

  await page.goto("/");
  await page.getByPlaceholder("e.g. aludan").fill("playwright-project-editor");
  await page.getByRole("button", { name: "Continue to workspace" }).click();

  await page.getByRole("button", { name: "New project" }).click();
  const newProjectDialog = page.getByRole("dialog", { name: "New Project" });
  await newProjectDialog.getByRole("textbox", { name: "Project name" }).fill(projectName);
  await newProjectDialog.getByRole("button", { name: "Create project" }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${projectId}/new$`));

  try {
    await page.getByTitle(`Project actions for ${projectName}`).click();
    await page.getByRole("menuitem", { name: "Import", exact: true }).click();
    const emptyImportDialog = page.getByRole("dialog", { name: "Import Project Contents" });
    await expect(emptyImportDialog.getByRole("checkbox", { name: /Replace all current project content/ })).toHaveCount(0);
    await emptyImportDialog.getByRole("button", { name: "Close" }).click();

    await page.getByTitle(`Project actions for ${projectName}`).click();
    await page.getByRole("menuitem", { name: "Contents", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Project Contents" });
    await expect(dialog).toBeVisible();

    await dialog.getByRole("button", { name: "Actions for data" }).click();
    await page.getByRole("menuitem", { name: "New folder", exact: true }).click();
    const folderDialog = page.getByRole("dialog", { name: "New folder" });
    await folderDialog.getByRole("textbox", { name: "Folder name" }).fill("reports");
    await folderDialog.getByRole("button", { name: "Create folder" }).click();
    await expect(dialog.getByText("reports was created.")).toBeVisible();

    await dialog.getByRole("button", { name: "Actions for reports" }).click();
    await page.getByRole("menuitem", { name: "Add file", exact: true }).click();
    await dialog.locator('input[type="file"]').setInputFiles({
      name: "notes.md",
      mimeType: "text/markdown",
      buffer: Buffer.from("# First version\n\nCreated from the project explorer."),
    });
    await expect(dialog.getByText("notes.md was added.")).toBeVisible();
    await expect(dialog.getByRole("heading", { name: "First version" })).toBeVisible();

    await dialog.getByRole("button", { name: "Replace", exact: true }).click();
    await dialog.locator('input[type="file"]').setInputFiles({
      name: "notes.md",
      mimeType: "text/markdown",
      buffer: Buffer.from("# Second version\n\nThe replacement is visible."),
    });
    const replaceDialog = page.getByRole("dialog", { name: "Replace file" });
    await replaceDialog.getByRole("button", { name: "Replace file" }).click();
    await expect(dialog.getByText("notes.md was replaced.")).toBeVisible();
    await expect(dialog.getByRole("heading", { name: "Second version" })).toBeVisible();

    await dialog.getByRole("button", { name: "Delete", exact: true }).click();
    const deleteFileDialog = page.getByRole("dialog", { name: "Delete file?" });
    await expect(deleteFileDialog.getByText("This cannot be undone.")).toBeVisible();
    await deleteFileDialog.getByRole("button", { name: "Delete permanently" }).click();
    await expect(dialog.getByText("notes.md was deleted.")).toBeVisible();

    await dialog.getByRole("button", { name: "Actions for reports" }).click();
    await page.getByRole("menuitem", { name: "Delete folder", exact: true }).click();
    const deleteFolderDialog = page.getByRole("dialog", { name: "Delete folder?" });
    await deleteFolderDialog.getByRole("button", { name: "Delete permanently" }).click();
    await expect(dialog.getByText("reports was deleted.")).toBeVisible();
  } finally {
    const cleanupResponse = await page.context().request.delete(`/api/projects/${encodeURIComponent(projectId)}`);
    expect(cleanupResponse.ok()).toBeTruthy();
  }
});
