# LinuxDD TG 监控器

这是一个基于 `DrissionPage + curl-cffi + Telegram Bot` 的 Discourse 监控程序。  
Docker Compose 模式会通过 noVNC 暴露真实 Chromium，手动完成登录和人机验证后，程序会持续拉取新帖并推送到 Telegram。
未复用登录态或运行中取帖失败时，可把 noVNC 地址发到指定 Telegram 用户，等待人工处理 Cloudflare 或重新登录。

## 功能

- 多站点监控 `new.json`
- 基础过滤、分类过滤、时间过滤
- 全量频道推送 + 关键词订阅推送
- SQLite 去重与旧数据清理
- 站点代理和 Telegram 代理分离
- Docker Compose 下暴露 noVNC，支持远程手动登录
- 启动登录和取帖失败时发送 noVNC 人工处理通知

## 运行方式

直接运行只启动 Python 监控和本机 Chromium，不会启动 noVNC、VNC 或 websockify。需要从外部浏览器访问登录页时，请使用下面的 Docker / noVNC 方式。

```bash
uv venv
uv pip install -r requirements.txt
uv run main.py -c config.yaml
```

单次检查：

```bash
uv run main.py --check-once
```

## Docker / noVNC

```bash
docker compose up -d --build
docker compose logs -f linuxdd-monitor
```

访问：

```text
http://服务器IP:6080/vnc.html
```

在 noVNC 里登录 `https://linux.do/login`。`manual_browser` 模式不会自动填账号密码，只会等待你手动完成登录。
noVNC/VNC 的开关、密码、端口和公开地址都在 `config.yaml` 的 `global.novnc` 配置，不再通过环境变量设置。设置 `global.novnc.enabled: false` 时只启动 Xvfb 和监控程序，不暴露 noVNC，也不要求 `global.novnc.password`。

启动流程：

1. Docker 先启动 Xvfb、Chromium 桌面环境和 noVNC。
2. 程序访问 `login_url` 检测登录态；已登录会直接进入监控。
3. 未登录时，`telegram.manual_review` 会向指定用户发送 noVNC 地址。
4. 你在 noVNC 里完成登录或 Cloudflare 验证后，程序继续监控推送。

运行中如果 `/new.json` 取帖失败或返回 403，程序会把浏览器打开到站点首页 `https://linux.do`，发送同一个 noVNC 地址给人工处理用户，等待你完成 Cloudflare 验证或重新登录，然后自动重试一次。本轮仍失败时会记录真实错误并等下一轮调度。

## 配置要点

- `sites.linuxdo.auth.mode: manual_browser`：强制手动登录
- `sites.linuxdo.remote_browser.enabled: true`：切换为远程/手动浏览器登录模式
- `sites.linuxdo.browser.headless: false`：必须保留，否则看不到界面
- `sites.linuxdo.browser.disable_infobars: true`：隐藏 Chromium 启动参数警告条
- `sites.linuxdo.browser.no_sandbox: true`：Docker/root 运行通常需要；非 root 运行可设为 `false`
- `global.novnc.enabled: true`：Docker Compose 中暴露 noVNC；关闭后不会监听 noVNC 端口
- `global.novnc.password`：noVNC/VNC 访问密码，启用 noVNC 时必须填写
- `global.novnc.port: 6080`：noVNC Web 端口，访问地址默认为 `http://服务器IP:6080/vnc.html`
- `global.novnc.public_host` / `global.novnc.public_url`：发送到 Telegram 的外部 noVNC 地址；`public_url` 非空时优先使用
- `global.novnc.login_wait_timeout: 1200`：手动登录等待超时秒数
- `sites.linuxdo.telegram.manual_review.enabled: true`：启用登录/取帖失败人工处理通知
- `sites.linuxdo.telegram.manual_review.chat_id`：显式指定人工处理 TG 用户
- `sites.linuxdo.telegram.manual_review.chat_id_from`：可设为 `first_keyword_subscriber` 或 `channel_id`
- `sites.linuxdo.proxy`：站点专用代理；启用时优先于全局站点代理
- `global.proxy`：Telegram 代理
- `global.site_proxy`：站点请求代理；站点专用代理未启用时自动继承

`config.example.yaml` 已按当前结构提供模板，复制后填写 Telegram Token、Chat ID 和实际订阅词即可。
示例配置中的代理默认关闭，代理地址默认保留为 `127.0.0.1:7890`。
真实 `config.yaml` 可按需开启 `sites.<site>.proxy` 或 `global.site_proxy`。
如果站点里写了 `proxy.enabled: false`，表示不使用站点专用代理，仍会继承已启用的 `global.site_proxy`。

`config.yaml`、`chrome_profile/`、`data/` 和 `logs/` 包含本机登录态或运行数据，已加入 `.gitignore` 和 `.dockerignore`。构建 Docker 镜像时不会把这些文件复制进镜像；不要把真实 Token、Cookie 或浏览器 profile 写入源码或示例配置。

## 输出

- `data/topics.db`：帖子与每个推送目标的送达状态
- `logs/monitor.log`：运行日志
