import { expect, test } from '@playwright/test'

// Smoke: log in, create a dry-run bot through the wizard, see it on the dashboard.
test('operator can log in and create a dry-run bot', async ({ page }) => {
  await page.goto('/')

  // Redirected to login when unauthenticated.
  await expect(page.getByRole('heading', { name: 'Trading Console' })).toBeVisible()
  await page.getByLabel('Username').fill('operator')
  await page.getByLabel('Password').fill('e2e-pass')
  await page.getByRole('button', { name: 'Sign in' }).click()

  // Dashboard loads.
  await expect(page.getByRole('link', { name: 'New bot' })).toBeVisible()

  // Walk the wizard.
  await page.getByRole('link', { name: 'New bot' }).click()
  await expect(page.getByRole('heading', { name: /step 1 of 4/i })).toBeVisible()
  await page.getByRole('button', { name: 'Next' }).click() // venue -> strategy
  await page.getByRole('button', { name: 'Next' }).click() // strategy -> params

  const unique = `E2E${Date.now()}/USD`
  await page.getByLabel('Symbol').fill(unique)
  await page.getByLabel('API key', { exact: true }).fill('demo-key')
  await page.getByLabel('API secret').fill('demo-secret')
  await page.getByRole('button', { name: 'Next' }).click() // params -> review

  // Dry-run by default, then create.
  await expect(page.getByText('DRY-RUN', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Create bot' }).click()

  // Landed on the new bot's detail page.
  await expect(page.getByRole('heading', { name: new RegExp(unique.replace('/', '\\/')) })).toBeVisible()

  // And it shows on the dashboard.
  await page.getByRole('link', { name: 'Dashboard' }).click()
  await expect(page.getByRole('link', { name: unique })).toBeVisible()
})
