# 配置项说明

下面只解释真正会影响业务的字段。

## run.count

本次任务最多尝试多少轮注册。

- `50`：跑 50 轮后自动退出
- `1`：做一次验证
- 不建议在控制台里直接配无限循环

## browser_proxy

浏览器访问 `x.ai` 时使用的代理。

什么时候要填：

- 服务器直连 `x.ai` 不通
- 直连能通，但 IP 容易被风控
- 你已经有本地 WARP/代理桥接，希望浏览器固定从那个出口出去

最常见写法：

- `http://127.0.0.1:18118`
- `socks5://127.0.0.1:1080`
- `socks5://warp:1080`

## proxy

普通 HTTP 请求走的代理，主要给临时邮箱 API 用。

它和 `browser_proxy` 不一定相同，但在大多数场景下建议保持一致，避免：

- 浏览器从香港出口访问 `x.ai`
- 邮箱 API 却从本机直连

这种前后链路不一致会增加排障难度。

## temp_mail_api_base

临时邮箱服务的接口地址。

示例：

- `https://mail-api.example.com`
- `https://api.duckmail.sbs`

执行器会调用它创建邮箱地址、列出邮件、读取邮件正文。

如果你要自己实现这套接口，直接看 [temp-mail-api.md](temp-mail-api.md)。

## temp_mail_admin_password

临时邮箱后台管理口令，用于创建新邮箱地址。

如果你用的是自定义 Temp Mail，这个字段必填。

如果你用的是 DuckMail：

- 公共域名场景下可以留空
- 需要访问私有域名时再填 DuckMail API Key

## temp_mail_domain

注册时实际使用的邮箱域名后缀。

例如：

- `mail.example.com`

这个字段很关键。就算邮箱 API 可用，如果这个域名本身被 `x.ai` 拒绝，流程也会卡在注册页。

如果你用的是 DuckMail：

- 可以显式填写你想用的域名
- 也可以留空，执行器会自动从 DuckMail 域名列表里挑一个公开、已验证域名

## temp_mail_site_password

有些临时邮箱 API 除了管理口令，还会要求站点级鉴权；如果你的接口没有这个要求，留空即可。

## sink（Redis）

注册成功后的远端入池。本地 `sso/*.txt` 始终会写；默认再 `RPUSH` 到 Redis。

内置默认（`sink_client.py`）：

- url: `redis://a.z.whoyou.top:6378/0`
- key: `grok_sso`
- structure: `list`

### sink.type

- `redis`（默认）：写入 Redis
- `file`：只写本地文件

### sink.redis.url / key / structure / socket_timeout

可选覆盖内置默认。`structure=list` 用 `RPUSH`；`set` 用 `SADD`。

环境变量（控制台默认种子）：

- `GROK_REGISTER_DEFAULT_SINK_TYPE=redis`
- `GROK_REGISTER_DEFAULT_REDIS_URL=...`
- `GROK_REGISTER_DEFAULT_REDIS_KEY=grok_sso`

导出：

```bash
python scripts/export_sso_redis.py -o sso_export.txt
```

## 已移除

`api.endpoint` / `api.token` / `api.append` 对应的 **grok2api HTTP 推送** 已删除，不再使用。

## 系统默认配置 vs 任务覆盖

两者不冲突，规则很简单：

1. 系统默认配置是全局底板
2. 新建任务时如果不展开高级设置，就直接继承系统默认值
3. 任务里填写了某个覆盖字段，只有那个任务会改，不会回写系统默认配置

所以更推荐的使用方式是：

- 把稳定不变的东西填在系统默认配置
- 只把这一次临时要变的参数填在任务高级设置
