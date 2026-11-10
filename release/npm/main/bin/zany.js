#!/usr/bin/env node
// zany 启动器: 启动 postinstall 下载到本包 bin/ 下的平台二进制。
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const suffix = process.platform === "win32" ? ".exe" : "";
const target = `${process.platform}-${process.arch}`;
const bin = path.join(__dirname, `zany-${target}${suffix}`);
if (!fs.existsSync(bin)) {
  console.error(`zany-agent: 平台二进制不存在: ${target}`);
  console.error("请重新执行 npm install，或检查 GitHub Releases 是否存在对应版本的二进制附件。");
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
