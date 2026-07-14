# token-sink

结果落池模块。当前唯一远端路径是 **Redis LIST**。

注册成功后执行器会：

1. 把 `sso` 追加写入本地 `sso/*.txt`（始终执行）
2. `RPUSH` 到 Redis key `grok_sso`（默认）

## 默认（写死在 sink_client.py）

| 项 | 值 |
|----|-----|
| URL | `redis://a.z.whoyou.top:6378/0`（无密码） |
| Key | `grok_sso` |
| 结构 | LIST（`RPUSH`） |

`config.json` 的 `sink.redis` 可覆盖 url/key/structure/timeout。  
`sink.type=file` 时跳过远端，只写本地文件。

## 导出

```bash
python scripts/export_sso_redis.py -o sso_export.txt
# 或
redis-cli -h a.z.whoyou.top -p 6378 LRANGE grok_sso 0 -1
```

## 实现

- `sink_client.push_to_redis` / `dispatch_sink`
- 执行器：`DrissionPage_example.push_sso_to_api`（每成功一轮即时推送）

**已移除**：grok2api HTTP（`/admin/api/tokens/add`）推送路径。
