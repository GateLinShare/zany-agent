#!/usr/bin/env node
// zany 启动器: 选平台二进制 spawn, stdio 直通, 退出码透传。零依赖 CommonJS。
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const suffix = process.platform === "win32" ? ".exe" : "";
let bin;
try {
  bin = require.resolve(`zany-agent-${process.platform}-${process.arch}/bin/zany${suffix}`);
} catch {
  console.error(`zany-agent: 平台包缺失 (${process.platform}-${process.arch} 不在支持列表: linux-x64 / darwin-arm64 / win32-x64)`);
  process.exit(1);
}

// 首跑: ~/.zany/.env 缺失时从 npm 包里铺一份 env.example, 提示填密钥
const envPath = path.join(os.homedir(), ".zany", ".env");
if (!fs.existsSync(envPath)) {
  const tpl = path.join(__dirname, "..", "env.example");
  if (fs.existsSync(tpl)) {
    fs.mkdirSync(path.dirname(envPath), { recursive: true });
    fs.copyFileSync(tpl, envPath);
    console.error(`zany: 已生成 ${envPath} — 填入 ANTHROPIC_API_KEY 后重跑`);
    process.exit(1);
  }
}

const child = spawn(bin, process.argv.slice(2), { stdio: "inherit" });
child.on("error", (e) => { console.error(`zany-agent: 启动失败: ${e.message}`); process.exit(1); });
child.on("exit", (code) => process.exit(code ?? 1));
