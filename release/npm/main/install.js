#!/usr/bin/env node
// npm 安装器：从对应版本的 GitHub Release 下载当前平台二进制。
// 不依赖第三方 npm 包，避免 postinstall 阶段还需要解析依赖。
const fs = require("fs");
const https = require("https");
const path = require("path");

const pkg = require("./package.json");
const owner = "GateLinShare";
const repo = "zany-agent";
const suffix = process.platform === "win32" ? ".exe" : "";
const target = `${process.platform}-${process.arch}`;
const supported = new Set(["linux-x64", "darwin-arm64", "win32-x64"]);

if (!supported.has(target)) {
  console.error(`zany-agent: 暂不支持平台 ${target}，支持：linux-x64 / darwin-arm64 / win32-x64`);
  process.exit(1);
}

const version = pkg.version;
const asset = `zany-${target}${suffix}`;
const url = `https://github.com/${owner}/${repo}/releases/download/v${version}/${asset}`;
const output = path.join(__dirname, "bin", asset);

function download(location, redirects = 0) {
  if (redirects > 5) throw new Error("下载重定向次数过多");
  return new Promise((resolve, reject) => {
    https.get(location, { headers: { "User-Agent": "zany-agent-npm-installer" } }, (response) => {
      if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
        response.resume();
        download(response.headers.location, redirects + 1).then(resolve, reject);
        return;
      }
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error(`HTTP ${response.statusCode}`));
        return;
      }
      fs.mkdirSync(path.dirname(output), { recursive: true });
      const stream = fs.createWriteStream(output, { mode: 0o755 });
      response.pipe(stream);
      stream.on("finish", () => stream.close(resolve));
      stream.on("error", reject);
      response.on("error", reject);
    }).on("error", reject);
  });
}

console.log(`zany-agent: 下载 ${asset} (v${version})`);
download(url).then(() => {
  if (process.platform !== "win32") fs.chmodSync(output, 0o755);
  console.log(`zany-agent: 已安装到 ${output}`);
}).catch((error) => {
  try { fs.rmSync(output, { force: true }); } catch {}
  console.error(`zany-agent: 下载失败：${error.message}`);
  console.error(`请确认 GitHub Release v${version} 已上传附件 ${asset}。`);
  process.exit(1);
});
