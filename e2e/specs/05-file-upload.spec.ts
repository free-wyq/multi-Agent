/**
 * 任务20b 流程5 — 文件上传（方案A：前端读 .md/.txt 文本拼进消息发送）。
 *
 * 覆盖：
 *  - 点上传按钮（aria-label="上传文件"）→ antd Upload 触发 OS 文件选择 → setInputFiles 注入。
 *  - beforeUpload 读 File.text() → 拼成「📄 filename.md\n<内容>」→ handleSendMessage 直接发送。
 *  - 用户气泡出现文件内容 + mock LLM 回复探针串。
 *  - 非法扩展（.pdf）→ toast「不支持的文件类型」+ 不发送。
 *
 * 任务8 实现：零后端端点（beforeUpload 读文本拼进 chatInput 直发，无 multipart/无存储）。
 */
import { expect, test } from '@playwright/test'
import path from 'node:path'
import fs from 'node:fs'
import os from 'node:os'

test.describe('流程5 — 文件上传', () => {
  test('上传 .md 文件 → 内容作为消息发送 → 收 mock 回复', async ({ page }) => {
    // 准备一个临时 .md 文件（< 100KB）。
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'e2e-upload-'))
    const mdPath = path.join(tmpDir, 'e2e-notes.md')
    const mdContent = '# e2e 上传测试\n这是上传文件的内容探针。'
    fs.writeFileSync(mdPath, mdContent, 'utf-8')

    try {
      await page.goto('/')
      await page.locator('.ant-layout-sider').getByText('前端工程师', { exact: true }).click()
      await expect(page.getByPlaceholder(/输入消息/)).toBeVisible({ timeout: 5000 })

      // 点上传按钮（aria-label="上传文件"），同时 setInputFiles 到隐藏的 input[type=file]。
      // antd Upload 渲染一个隐藏 input[type=file]，点击触发按钮会打开 OS 选择器——
      // 在 Playwright 里直接 setInputFiles 注入文件即可触发 beforeUpload。
      await page.setInputFiles('input[type="file"]', mdPath)

      // 上传即发送：用户气泡出现（含文件名标题 + 内容探针）。
      await expect(
        page.locator('.chat-msg').filter({ hasText: 'e2e-notes.md' }),
      ).toBeVisible({ timeout: 10_000 })
      await expect(
        page.locator('.chat-msg').filter({ hasText: '上传文件的内容探针' }),
      ).toBeVisible()

      // mock LLM 回复探针串出现。
      await expect(
        page.locator('.chat-msg').filter({ hasText: '[e2e-mock]' }).first(),
      ).toBeVisible({ timeout: 30_000 })
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true })
    }
  })

  test('上传 .pdf 非法扩展 → toast 拒绝 + 不发送', async ({ page }) => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'e2e-upload-'))
    const pdfPath = path.join(tmpDir, 'e2e-doc.pdf')
    // 伪 pdf 内容（beforeUpload 只看扩展名，不验内容）。
    fs.writeFileSync(pdfPath, '%PDF-1.4 fake', 'utf-8')

    try {
      await page.goto('/')
      await page.locator('.ant-layout-sider').getByText('前端工程师', { exact: true }).click()
      await expect(page.getByPlaceholder(/输入消息/)).toBeVisible({ timeout: 5000 })
      // 记录当前消息数（确认不新增）。
      const beforeCount = await page.locator('.chat-msg').count()

      await page.setInputFiles('input[type="file"]', pdfPath)

      // toast「不支持的文件类型」出现。
      await expect(page.getByText(/不支持的文件类型/)).toBeVisible({ timeout: 10_000 })
      // 不发送——消息数不应增加（给点时间确认）。
      await page.waitForTimeout(1500)
      const afterCount = await page.locator('.chat-msg').count()
      expect(afterCount).toBe(beforeCount)
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true })
    }
  })
})
