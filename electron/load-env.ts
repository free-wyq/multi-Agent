/**
 * 轻量 .env 加载器（无需额外依赖）
 * 开发/生产环境自动在项目根目录查找 .env 文件
 */
import * as fs from 'fs'
import * as path from 'path'

const candidates = [
  path.join(process.cwd(), '.env'),               // 开发模式
  path.join(__dirname, '..', '..', '.env'),       // 打包后 dist-electron/ -> ../.. 回到根
]

for (const fp of candidates) {
  if (fs.existsSync(fp)) {
    const text = fs.readFileSync(fp, 'utf-8')
    for (const line of text.split('\n')) {
      const trimmed = line.trim()
      if (!trimmed || trimmed.startsWith('#')) continue
      const eq = trimmed.indexOf('=')
      if (eq === -1) continue
      const key = trimmed.slice(0, eq).trim()
      let val = trimmed.slice(eq + 1).trim()
      // 去掉引号
      if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
        val = val.slice(1, -1)
      }
      // 只设置未定义的环境变量
      if (key && process.env[key] === undefined) {
        process.env[key] = val
      }
    }
    break
  }
}
