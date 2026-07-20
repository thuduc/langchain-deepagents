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
