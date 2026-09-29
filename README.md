# openurl

扫码把手机上的网址甩给车机。

> **⚠️ 这是 AI 写的「日抛代码」**
>
> 能跑、够用，但没打算长期维护，也不保证风格一致。
> 发现 bug 或想加功能，**直接把它丢给 AI 让它改就行**，不用等我。

## 这是什么

在车机浏览器里输入网址是件痛苦的事：虚拟键盘难打、长链接容易敲错、还不一定能复制粘贴。
openurl 用「房间号」把手机和车机临时配对，手机上输入、车机上打开：

1. 车机浏览器打开 `openurl.cgi`，自动建房，显示 4 位房间号 + 二维码
2. 手机扫码进入同一个房间
3. 手机上粘贴或输入网址，点「发送到车机」
4. 车机端 2 秒内收到，并自动跳转打开

## 📦 安装

> **就两步**：装一个 Python 依赖，把 `openurl.cgi` 放到 Apache 或 nginx 下。

### 1. 安装依赖

需要 Python 3.6+。整个项目只有一个依赖：

```bash
pip install "qrcode[pil]"
```

`qrcode[pil]` 会连同 Pillow 一起装上，用于在服务端生成二维码 PNG。
不装也不影响收发网址，只是二维码接口会返回 500。

### 2. 把 CGI 放到 Apache 或 nginx 下

```bash
chmod +x openurl.cgi                 # CGI 必须有执行权限
cp openurl.cgi /var/www/cgi-bin/     # 目录按你自己站点的配置改
```

**Apache** —— 开箱支持 CGI：

```apache
ScriptAlias /openurl /var/www/cgi-bin/openurl.cgi
<Directory /var/www/cgi-bin>
    Options +ExecCGI
    AddHandler cgi-script .cgi
    Require all granted
</Directory>
```

**nginx** —— nginx 自己不能执行 CGI，需要 fcgiwrap：

```bash
apt install fcgiwrap                 # Debian/Ubuntu；macOS 用 brew install fcgiwrap
```

```nginx
location = /openurl.cgi {
    gzip off;
    include fastcgi_params;
    fastcgi_pass unix:/run/fcgiwrap.socket;
    fastcgi_param SCRIPT_FILENAME /var/www/cgi-bin/openurl.cgi;
    fastcgi_param SCRIPT_NAME /openurl.cgi;
}
```

### 3. 确认权限

- **执行权限**：上面 `chmod +x` 已经给了
- **写权限**：房间数据默认写在 `/tmp/openurl_rooms/`，脚本会自动创建，CGI 的运行用户
  （`www-data` / `_www` / `nginx`）要对它有写权限；想换位置就设 `OPENURL_ROOM_DIR`

配完访问 `http://你的地址/openurl.cgi` 就能看到车机端页面。

> **前面还有一层反向代理的话**：确保它传递 `X-Forwarded-Host` 和 `X-Forwarded-Proto`，
> 否则二维码里生成的是内网地址，手机扫码打不开。

## 特性

- 纯 CGI 实现：无长连接、无 WebSocket、无数据库，能穿过严格的企业代理和反向代理
- 服务器不长期存储：消息被车机取走即删除；房间 30 分钟无活动惰性清理
- 服务端生成二维码 PNG，不依赖前端 JS 二维码库
- 手机端历史记录存 localStorage，支持置顶、重发、同一网址自动去重
- 车机端接收记录同样本地保存，可置顶 / 删除
- 二维码里的链接按请求自身的 host / scheme 生成，内网、外网域名都可用

## 配置

二维码里的链接按请求自身的 `X-Forwarded-Host` / `Host` 和 `X-Forwarded-Proto` 推导，
内网访问就生成内网地址，外网域名访问就生成外网地址。**正常部署不需要配任何东西。**

以下都是可选环境变量，不设也能正常工作：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPENURL_ROOM_DIR` | `/tmp/openurl_rooms` | 存放房间 JSON 的目录，由脚本自动创建 |
| `OPENURL_DEFAULT_HOST` | `localhost` | 取不到 `HTTP_HOST` 时，二维码链接使用的兜底主机名 |
| `OPENURL_HTTPS_HOSTS` | 空 | 逗号分隔的主机名片段，命中即强制用 `https` 生成链接 |

其中后两个只在反向代理没传 `X-Forwarded-*`（或者你需要固定域名）时才需要配。

Apache 用 `SetEnv`：

```apache
SetEnv OPENURL_ROOM_DIR /var/lib/openurl/rooms
SetEnv OPENURL_DEFAULT_HOST example.com:8080
SetEnv OPENURL_HTTPS_HOSTS example.com,.myds.me
```

nginx + fcgiwrap 用 `fastcgi_param`：

```nginx
fastcgi_param OPENURL_ROOM_DIR /var/lib/openurl/rooms;
fastcgi_param OPENURL_DEFAULT_HOST example.com:8080;
fastcgi_param OPENURL_HTTPS_HOSTS example.com,.myds.me;
```

## 接口

除 `action=send` / `join` / `leave` 外都是 GET。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `openurl.cgi` | 入口页：读本地房间号，没有就新建 |
| GET | `openurl.cgi?new=1` | 强制新建房间并跳转 |
| GET | `openurl.cgi?room=1234` | 车机端页面 |
| GET | `openurl.cgi?room=1234&m=1` | 手机端页面（扫码进入） |
| GET | `openurl.cgi?action=qr&room=1234` | 二维码 PNG |
| GET | `openurl.cgi?action=poll&room=1234&since=N` | 车机短轮询，返回后消息即删除 |
| POST | `openurl.cgi?action=send&room=1234` | 发送网址，body: `url=...` |
| POST | `openurl.cgi?action=join&room=1234&pid=...` | 手机心跳，用于统计在线手机数 |
| POST | `openurl.cgi?action=leave&room=1234&pid=...` | 手机离开，在线数即时归零 |

轮询返回示例：

```json
{
  "status": "ok",
  "data": {
    "msgs": [{"seq": 3, "type": "url", "url": "https://example.com", "time": 1759100000}],
    "seq": 3,
    "room_status": {"phone_count": 1}
  }
}
```

## 已知问题 / TODO

- 房间号只有 4 位数字，没有鉴权。任何人都可能猜中房间号并投递网址，
  仅适合临时、非敏感的用途；如需更强隔离建议换成更长的随机房间码
- 消息存放在 `/tmp`，重启即清空（这也是设计意图）
- 没有请求频率限制

## License

MIT
